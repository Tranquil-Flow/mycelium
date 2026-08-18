#!/usr/bin/env python3
"""Preposition an existing authorized live route through member artifact transport."""
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_live.artifact_provisioner import ArtifactAcquisitionStore
from mycelium_live.local_preparer import LocalCandidatePreparer
from mycelium_live.member_transport import MemberArtifactTransport
from runtime_loader import canonical_json


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path.resolve(strict=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-plan", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--member-transport-plan", type=Path, required=True)
    parser.add_argument("--artifact-store-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--seed-state-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    plan = json.loads(args.operator_plan.read_text("utf-8"))
    authorization = json.loads(args.authorization.read_text("utf-8"))
    workspace = _private_directory(args.workspace_root)
    candidates = _private_directory(workspace / "candidates")
    attempt = _private_directory(workspace / ("preposition-" + uuid.uuid4().hex[:12]))
    transport = MemberArtifactTransport(args.member_transport_plan)
    preparer = LocalCandidatePreparer(
        repo_root=ROOT,
        cache_root=Path.home() / ".cache" / "huggingface" / "hub",
        template_plan=args.operator_plan,
        workspace_root=workspace / "preparer",
        candidate_root=candidates,
        seed_state_root=args.seed_state_root,
        artifact_store=ArtifactAcquisitionStore(args.artifact_store_root),
        member_stage_pack_acquirer=transport,
        python_executable=sys.executable,
    )
    preparer._preflight_local_acquisition_storage(  # noqa: SLF001
        authorization,
        {"nodes": plan["controller"]["peers"]},
        attempt=attempt,
    )
    preparer._acquire_stage_packs(  # noqa: SLF001 -- exact product preparation seam.
        attempt=attempt,
        authorization=authorization,
        plan=plan,
    )
    args.output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(canonical_json(plan), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "protocol": "mycelium.live_route_preposition_result.v1",
                "operator_plan": str(args.output),
                "members": sorted(
                    plan["controller"]["prepositioned_artifacts"]["members"]
                ),
                "route_ready": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
