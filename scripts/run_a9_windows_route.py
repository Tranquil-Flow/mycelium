#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A9 two-host managed route (M4 Pro macOS + native Windows): owner-side driver.

This drives the A13 owner side directly for the A9 route model
(``A9_ROUTE_MODEL``: Qwen2.5-0.5B at the exact revision, int8-weight-only), with
each managed node on its own class's runtime (mlx on macOS, numpy on Windows).

Subcommands:

  compile-check   Compile (never run) a mixed mlx+numpy managed plan for the
                  local 0.5B snapshot from synthetic but well-formed inputs:
                  fresh template, two-node managed topology (one macOS mlx
                  node with physical POSIX paths, one Windows numpy node with
                  logical /mycelium-managed paths), fresh hello observations
                  and real public keys.  Proves the builder and the owner's
                  registry launch accept the route before the run day.
  compile         Run day: compile from the two verified installation
                  descriptors and the live hello observations the owner
                  collected (JSON inputs written by the owner app), then
                  validate the registry launch with per-node runtimes.

Nothing here opens a route, loads weights into a node, or claims readiness.
"""

from __future__ import annotations

import argparse
from argparse import Namespace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SNAPSHOT = (
    Path.home()
    / "mycelium-wp8-01/owner-lhr/model-source/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots"
    / "7ae557604adf67be50417f59c2c2f167def9a775"
)


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _write(path: Path, document) -> Path:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _hex_id(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _activation_endpoint_id(signer) -> str:
    """Iroh identifies an endpoint by its Ed25519 public key (as the descriptor verifier does)."""

    import base64

    return base64.b64decode(signer.public_key_record()["verification_key"], validate=True).hex()


def compile_route(*, work: Path, snapshot: Path, topology: dict, observations: dict, keys: dict,
                  deployment_id: str, seed_state_root: Path | None = None) -> dict:
    """Compile the managed plan and validate the owner's registry launch."""

    from mycelium_a13_macos.compiler_inputs import fresh_operator_template
    from mycelium_a13_macos.serving import A9_ROUTE_MODEL, prepare_local_source, prepare_registry_launch
    from scripts.build_qwen_live_route import build

    root = _private_dir(work)
    template = fresh_operator_template(root / "compiled", keys)
    args = Namespace(
        template_plan=_write(root / "template.json", template),
        model_root=snapshot,
        model_id=A9_ROUTE_MODEL.model_id,
        resolved_commit=A9_ROUTE_MODEL.resolved_commit,
        deployment_id=deployment_id,
        output_root=root / "compiled",
        route_label="a9win",
        topology=_write(root / "topology.json", topology),
        managed_transport=True,
        managed_hello_observations=_write(root / "observations.json", observations),
        stage_sharded=False,
        reuse_stage_sharded_root=None,
        temporary_root=_private_dir(root / "temporary"),
        m13_control_plane=None,
        m14_control_plane=None,
        m18_control_plane=None,
        model_preparation_authorization=None,
    )
    started = time.time()
    build(args)
    plan = root / "compiled" / "operator-plan.json"
    prepared = prepare_local_source(snapshot, model=A9_ROUTE_MODEL)
    backends = {node["node_id"]: node["runtime_backend"] for node in topology["nodes"]}
    if seed_state_root is None:
        # The compile check has no live owner seed: validate exactly the plan
        # checks the registry launch applies (model pin + per-node runtimes).
        from mycelium_a13_macos import serving

        serving._validate_operator_plan(plan, prepared, backends)  # noqa: SLF001
        plan_digest = serving._plan_digest(plan)  # noqa: SLF001
    else:
        launch = prepare_registry_launch(
            prepared=prepared, operator_plan=plan, private_state_root=root / "registry",
            seed_state_root=seed_state_root, node_runtime_backends=backends)
        plan_digest = launch.operator_plan_digest
    document = json.loads(plan.read_text())
    assignments = {}
    bundle = Path(document["controller"]["source_root"])
    for node in document["controller"]["run_plan"]["nodes"]:
        assignment = json.loads((bundle / node["configure"]["assignment_file"]).read_text())
        assignments[node["node_id"]] = {
            "runtime": {k: assignment["runtime"][k] for k in ("backend", "dtype", "quantization")},
            "range": assignment["range"],
            "components": assignment["components"],
        }
    return {
        "protocol": "mycelium.a9_windows_route_compile.v1",
        "operator_plan_digest": plan_digest,
        "model_id": A9_ROUTE_MODEL.model_id,
        "resolved_commit": A9_ROUTE_MODEL.resolved_commit,
        "nodes": assignments,
        "compile_seconds": round(time.time() - started, 1),
        "claim_boundary": "compiled and validated only; no route opened, no node loaded",
    }


def cmd_compile_check(args: argparse.Namespace) -> int:
    from mycelium_qualification.signing import generate_ed25519_signer

    work = Path(args.work_dir).resolve()
    if work.exists():
        print(f"refusing to reuse {work}", file=sys.stderr)
        return 2
    seed = generate_ed25519_signer(endpoint_id="a9-check-seed")
    mac = generate_ed25519_signer(endpoint_id="a9-check-mac")
    win = generate_ed25519_signer(endpoint_id="a9-check-win")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"a9win-qwen-{stamp}"
    deployment_id = str(uuid.uuid4())
    mac_root = str(work / "mac-target")
    mac_id, win_id = ("node-b-mac", "node-a-win") if args.windows_entry else ("node-a-mac", "node-b-win")
    nodes = [
        {"node_id": mac_id, "process_transport": "managed_https", "ssh_target": None,
         "ssh_identity_file": None, "staging_root": f"{mac_root}/managed/{run_id}/mycelium-stage",
         "python_executable": sys.executable, "sidecar_binary": f"{mac_root}/sidecar",
         "endpoint_secret_file": f"{mac_root}/identity/node.key",
         "endpoint_id": _activation_endpoint_id(mac),
         "membership_generation": 3, "runtime_backend": "mlx"},
        {"node_id": win_id, "process_transport": "managed_https", "ssh_target": None,
         "ssh_identity_file": None,
         "staging_root": f"/mycelium-managed/{win_id}/{run_id}/mycelium-stage",
         "python_executable": f"/mycelium-managed/{win_id}/runtime/python",
         "sidecar_binary": f"/mycelium-managed/{win_id}/runtime/mycelium-iroh-sidecar",
         "endpoint_secret_file": f"/mycelium-managed/{win_id}/identity/node.key",
         "endpoint_id": _activation_endpoint_id(win),
         "membership_generation": 2, "runtime_backend": "numpy"},
    ]
    topology = {"protocol": "mycelium.qwen_live_topology.v1",
                "nodes": sorted(nodes, key=lambda node: node["node_id"])}
    observations = {
        "protocol": "mycelium.managed_hello_observations.v1",
        "deployment_id": deployment_id,
        "run_id": run_id,
        "captured_at_unix_ms": int(time.time() * 1000),
        "observations": [
            {"node_id": mac_id, "host_id": "host-" + _hex_id("check-mac-host"),
             "boot_id": "boot-" + _hex_id("check-mac-boot")},
            {"node_id": win_id, "host_id": "host-" + _hex_id("check-win-host"),
             "boot_id": "boot-" + _hex_id("check-win-boot")},
        ],
    }
    keys = {"gossip": [seed.public_key_record()],
            "load_proof": [mac.public_key_record(), win.public_key_record()]}
    result = compile_route(work=work, snapshot=Path(args.snapshot).resolve(), topology=topology,
                           observations=observations, keys=keys, deployment_id=deployment_id)
    _write(work / "compile-check.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    work = Path(args.work_dir).resolve()
    topology = json.loads(Path(args.topology).read_text())
    observations = json.loads(Path(args.observations).read_text())
    keys = json.loads(Path(args.keys).read_text())
    result = compile_route(work=work, snapshot=Path(args.snapshot).resolve(), topology=topology,
                           observations=observations, keys=keys, deployment_id=args.deployment_id,
                           seed_state_root=Path(args.seed_state_root).resolve())
    _write(work / "compile.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("compile-check")
    p.add_argument("--work-dir", required=True)
    p.add_argument("--windows-entry", action="store_true",
                   help="order the Windows node first so it holds the entry (embedding) stage")
    p.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    p.set_defaults(func=cmd_compile_check)
    p = sub.add_parser("compile")
    p.add_argument("--work-dir", required=True)
    p.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    p.add_argument("--topology", required=True, help="mycelium.qwen_live_topology.v1 from the verified descriptors")
    p.add_argument("--observations", required=True, help="mycelium.managed_hello_observations.v1 (live hello)")
    p.add_argument("--keys", required=True, help='{"gossip": [seed key record], "load_proof": [target key records]}')
    p.add_argument("--deployment-id", required=True)
    p.add_argument("--seed-state-root", required=True, help="the owner's live A13 seed state root")
    p.set_defaults(func=cmd_compile)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
