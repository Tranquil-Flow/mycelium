#!/usr/bin/env python3
"""Two-peer V1 demo for pinned minimal-shard Hugging Face provisioning."""
from __future__ import annotations

import argparse
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import model_manifest as mm
from layer_assignment import compile_layer_assignments, validate_target_cache_root
from route_contract import validate_manual_provisioning_route_v1
from weight_provisioning import audit_provisioning, provision_assignment


@dataclass(frozen=True)
class DemoNodeSpec:
   node_id: str
   start_layer: int
   end_layer_exclusive: int
   cache_root: str


def parse_node_specs(values: list[str]) -> list[DemoNodeSpec]:
   specs = []
   seen = set()
   for value in values:
      parts = value.split(",", 3)
      if len(parts) != 4:
         raise ValueError("node spec must be NODE_ID,START,END_EXCLUSIVE,ABSOLUTE_CACHE_ROOT")
      node_id, start_raw, end_raw, cache_root_raw = parts
      if not node_id or node_id in seen:
         raise ValueError(f"invalid or duplicate node_id: {node_id!r}")
      seen.add(node_id)
      try:
         start = int(start_raw)
         end = int(end_raw)
      except ValueError as exc:
         raise ValueError(f"node range must use integers: {value}") from exc
      cache_root = validate_target_cache_root(cache_root_raw, node_id)
      specs.append(DemoNodeSpec(node_id, start, end, cache_root))
   if not specs:
      raise ValueError("at least one --node is required")
   return specs


def build_demo_artifacts(
   manifest: dict[str, Any],
   node_specs: list[DemoNodeSpec],
   *,
   deployment_id: str,
   deployment_epoch: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
   route = []
   cache_roots = {}
   runtime_by_node = {}
   for spec in node_specs:
      route.append({
         "node_id": spec.node_id,
         "range": {
            "start_layer": spec.start_layer,
            "end_layer_exclusive": spec.end_layer_exclusive,
            "layer_count": spec.end_layer_exclusive - spec.start_layer,
         },
      })
      cache_roots[spec.node_id] = spec.cache_root
      runtime_by_node[spec.node_id] = {
         "backend": "artifact_verifier",
         "dtype": "source",
         "quantization": "none",
      }
   route_plan = {
      "ok": True,
      "protocol": "mycelium.manual_provisioning_route.v1",
      "model": {
         "model_id": manifest["model_id"],
         "num_layers": manifest["num_layers"],
         "resolved_commit": manifest["resolved_commit"],
         "manifest_digest": mm.manifest_digest_ref(manifest),
      },
      "route": route,
      "node_order": [spec.node_id for spec in node_specs],
      "claim_boundary": "manual demo allocation; allocator integration remains separate",
   }
   validate_manual_provisioning_route_v1(route_plan)
   assignments = compile_layer_assignments(
      route_plan=route_plan,
      manifest=manifest,
      deployment_id=deployment_id,
      deployment_epoch=deployment_epoch,
      cache_roots=cache_roots,
      runtime_by_node=runtime_by_node,
   )
   return route_plan, assignments


def _slug(node_id: str) -> str:
   value = re.sub(r"[^A-Za-z0-9_.-]+", "-", node_id).strip(".-")
   if not value:
      raise ValueError(f"node_id cannot form artifact filename: {node_id!r}")
   return value


def _write_json(path: Path, document: Any) -> str:
   path.parent.mkdir(parents=True, exist_ok=True)
   path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
   return str(path.resolve())


def write_orchestration(
   output_dir: Path,
   manifest: dict[str, Any],
   route_plan: dict[str, Any],
   assignments: list[dict[str, Any]],
) -> dict[str, Any]:
   output_dir = output_dir.expanduser().resolve()
   if not assignments:
      raise ValueError("orchestration requires at least one assignment")
   slug_by_node: dict[str, str] = {}
   node_by_slug: dict[str, str] = {}
   for assignment in assignments:
      node_id = assignment.get("node_id")
      if not isinstance(node_id, str) or not node_id or node_id in slug_by_node:
         raise ValueError(f"invalid or duplicate assignment node_id: {node_id!r}")
      slug = _slug(node_id)
      previous = node_by_slug.get(slug)
      if previous is not None:
         raise ValueError(
            f"assignment filename collision: node IDs {previous!r} and {node_id!r} both map to {slug!r}"
         )
      slug_by_node[node_id] = slug
      node_by_slug[slug] = node_id

   output_dir.mkdir(parents=True, exist_ok=True)
   manifest_path = _write_json(output_dir / "model-manifest.json", manifest)
   route_path = _write_json(output_dir / "manual-provisioning-route-v1.json", route_plan)
   assignment_paths = {}
   assignment_references = {}
   for assignment in assignments:
      node_id = assignment["node_id"]
      assignment_path = _write_json(
         output_dir / f"assignment-{slug_by_node[node_id]}.json",
         assignment,
      )
      assignment_paths[node_id] = assignment_path
      assignment_references[node_id] = Path(assignment_path).name
   deployment = {
      "protocol": "mycelium.weight_provisioning_demo.v1",
      "deployment_id": assignments[0]["deployment_id"],
      "deployment_epoch": assignments[0]["deployment_epoch"],
      "manifest": Path(manifest_path).name,
      "route": Path(route_path).name,
      "assignments": assignment_references,
      "claim_boundary": "orchestration artifacts only; run provision on every peer and audit reports",
   }
   deployment_path = _write_json(output_dir / "deployment.json", deployment)
   return {
      "manifest": manifest_path,
      "route": route_path,
      "assignments": assignment_paths,
      "deployment": deployment_path,
   }


def _read_json(path: str | Path) -> Any:
   return json.loads(Path(path).expanduser().read_text())


def command_orchestrate(args: argparse.Namespace) -> int:
   output_dir = Path(args.out_dir).expanduser().resolve()
   if output_dir.exists() and any(output_dir.iterdir()) and not args.force:
      raise ValueError(f"output directory is not empty: {output_dir}; pass --force to replace named artifacts")
   metadata_cache = Path(args.metadata_cache).expanduser().resolve() if args.metadata_cache else output_dir / "coordinator-hf-cache"
   manifest = mm.resolve_huggingface_manifest(
      args.repo,
      requested_revision=args.revision,
      cache_root=metadata_cache,
   )
   specs = parse_node_specs(args.node)
   deployment_id = args.deployment_id or str(uuid.uuid4())
   route, assignments = build_demo_artifacts(
      manifest,
      specs,
      deployment_id=deployment_id,
      deployment_epoch=args.deployment_epoch,
   )
   written = write_orchestration(output_dir, manifest, route, assignments)
   print(json.dumps({
      "ok": True,
      "resolved_commit": manifest["resolved_commit"],
      "manifest_digest": mm.manifest_digest_ref(manifest),
      "files": written,
   }, indent=2, sort_keys=True))
   return 0


def command_provision(args: argparse.Namespace) -> int:
   assignment = _read_json(args.assignment)
   report = provision_assignment(assignment, local_files_only=args.local_files_only)
   report_path = Path(args.report).expanduser().resolve()
   if report_path.exists() and not args.force:
      raise ValueError(f"report already exists: {report_path}; pass --force to replace")
   _write_json(report_path, report)
   print(json.dumps({
      "ok": True,
      "node_id": report["node_id"],
      "ready_for_load": report["ready_for_load"],
      "route_ready": report["route_ready"],
      "network_download_bytes": report["network_download_bytes"],
      "cache_hit_bytes": report["cache_hit_bytes"],
      "report": str(report_path),
   }, indent=2, sort_keys=True))
   return 0


def command_audit(args: argparse.Namespace) -> int:
   route = _read_json(args.route)
   assignments = [_read_json(path) for path in args.assignment]
   reports = [_read_json(path) for path in args.report]
   audit = audit_provisioning(route, assignments, reports)
   out_path = Path(args.out).expanduser().resolve()
   if out_path.exists() and not args.force:
      raise ValueError(f"audit already exists: {out_path}; pass --force to replace")
   _write_json(out_path, audit)
   print(json.dumps({**audit, "audit": str(out_path)}, indent=2, sort_keys=True))
   return 0 if audit["all_assignments_verified"] else 2


def main(argv: list[str] | None = None) -> int:
   parser = argparse.ArgumentParser(description="Mycelium pinned-shard provisioning demo")
   subparsers = parser.add_subparsers(dest="command", required=True)

   orchestrate = subparsers.add_parser("orchestrate", help="resolve model and write route/assignments")
   orchestrate.add_argument("--repo", required=True)
   orchestrate.add_argument("--revision", default="main")
   orchestrate.add_argument("--node", action="append", required=True, help="NODE_ID,START,END_EXCLUSIVE,ABSOLUTE_CACHE_ROOT")
   orchestrate.add_argument("--out-dir", required=True)
   orchestrate.add_argument("--metadata-cache")
   orchestrate.add_argument("--deployment-id")
   orchestrate.add_argument("--deployment-epoch", type=int, default=1)
   orchestrate.add_argument("--force", action="store_true")
   orchestrate.set_defaults(handler=command_orchestrate)

   provision = subparsers.add_parser("provision", help="download and verify one assignment")
   provision.add_argument("--assignment", required=True)
   provision.add_argument("--report", required=True)
   provision.add_argument("--local-files-only", action="store_true")
   provision.add_argument("--force", action="store_true")
   provision.set_defaults(handler=command_provision)

   audit = subparsers.add_parser("audit", help="audit all assignment reports")
   audit.add_argument("--route", required=True)
   audit.add_argument("--assignment", action="append", required=True)
   audit.add_argument("--report", action="append", required=True)
   audit.add_argument("--out", required=True)
   audit.add_argument("--force", action="store_true")
   audit.set_defaults(handler=command_audit)

   args = parser.parse_args(argv)
   return args.handler(args)


if __name__ == "__main__":
   raise SystemExit(main())
