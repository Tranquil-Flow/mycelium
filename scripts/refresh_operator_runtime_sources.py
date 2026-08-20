#!/usr/bin/env python3
"""Refresh explicitly named runtime sources in a generated physical operator plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("operator plan must contain an object")
    return value


def _safe_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if (
        not value
        or relative.is_absolute()
        or str(relative) != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"unsafe runtime source path: {value!r}")
    return relative


def _record(path: Path, relative: str) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": relative,
        "size_bytes": len(payload),
        "content_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }


def _replace_record(manifest: dict[str, Any], replacement: dict[str, object]) -> None:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("transfer manifest files are invalid")
    matches = [index for index, item in enumerate(files) if item.get("path") == replacement["path"]]
    if len(matches) != 1:
        raise ValueError(f"runtime source is not uniquely manifested: {replacement['path']}")
    files[matches[0]] = replacement


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-plan", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--source-root",
        type=Path,
        help="isolated transfer bundle to bind instead of mutating the plan's source",
    )
    parser.add_argument("--path", action="append", required=True)
    parser.add_argument("--existing-path", action="append", default=[])
    parser.add_argument("--staging-root", action="append", default=[])
    parser.add_argument("--output-plan", type=Path, required=True)
    args = parser.parse_args()

    plan = _load(args.operator_plan)
    controller = plan.get("controller")
    if not isinstance(controller, dict):
        raise ValueError("operator plan controller is invalid")
    source_root_value = controller.get("source_root")
    if not isinstance(source_root_value, str):
        raise ValueError("operator plan source root is invalid")
    source_root = (
        args.source_root.resolve(strict=True)
        if args.source_root is not None
        else Path(source_root_value).resolve(strict=True)
    )
    controller["source_root"] = str(source_root)
    repo_root = args.repo_root.resolve(strict=True)
    transfer_manifest = controller.get("transfer_manifest")
    node_manifests = controller.get("node_transfer_manifests")
    if not isinstance(transfer_manifest, dict) or not isinstance(node_manifests, dict):
        raise ValueError("operator plan transfer manifests are invalid")
    manifests = node_manifests.get("manifests")
    if not isinstance(manifests, dict):
        raise ValueError("operator plan node transfer manifests are invalid")

    requested_roots: dict[str, str] = {}
    for value in args.staging_root:
        node_id, separator, root = value.partition("=")
        if not separator or not node_id or not root or not Path(root).is_absolute():
            raise ValueError(f"invalid staging root override: {value!r}")
        if node_id in requested_roots:
            raise ValueError(f"duplicate staging root override: {node_id}")
        requested_roots[node_id] = root
    if requested_roots:
        peers = controller.get("peers")
        run_plan = controller.get("run_plan")
        run_nodes = run_plan.get("nodes") if isinstance(run_plan, dict) else None
        if not isinstance(peers, list) or not isinstance(run_nodes, list):
            raise ValueError("operator plan peer/run nodes are invalid")
        peer_by_node = {peer.get("node_id"): peer for peer in peers if isinstance(peer, dict)}
        run_by_node = {node.get("node_id"): node for node in run_nodes if isinstance(node, dict)}
        if set(requested_roots) - set(peer_by_node) or set(requested_roots) - set(run_by_node):
            raise ValueError("staging root override names an unknown node")
        for node_id, root in requested_roots.items():
            peer_by_node[node_id]["staging_root"] = root
            run_by_node[node_id]["socket_root"] = str(Path(root) / "socket")

    refreshed = []
    for value in args.path:
        relative = _safe_relative(value)
        source = repo_root.joinpath(*relative.parts).resolve(strict=True)
        if repo_root not in source.parents or not source.is_file():
            raise ValueError(f"runtime source is outside the repository: {value}")
        target = source_root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        replacement = _record(target, value)
        _replace_record(transfer_manifest, replacement)
        updated_nodes = []
        for node_id, manifest in sorted(manifests.items()):
            if not isinstance(manifest, dict):
                raise ValueError("node transfer manifest is invalid")
            files = manifest.get("files")
            if not isinstance(files, list):
                raise ValueError("node transfer manifest files are invalid")
            if any(item.get("path") == value for item in files):
                _replace_record(manifest, replacement)
                updated_nodes.append(node_id)
        if not updated_nodes:
            raise ValueError(f"runtime source is absent from all node manifests: {value}")
        refreshed.append({**replacement, "nodes": updated_nodes})

    for value in args.existing_path:
        relative = _safe_relative(value)
        target = source_root.joinpath(*relative.parts).resolve(strict=True)
        if source_root not in target.parents or not target.is_file():
            raise ValueError(f"existing source is outside the transfer root: {value}")
        replacement = _record(target, value)
        _replace_record(transfer_manifest, replacement)
        updated_nodes = []
        for node_id, manifest in sorted(manifests.items()):
            if not isinstance(manifest, dict):
                raise ValueError("node transfer manifest is invalid")
            files = manifest.get("files")
            if not isinstance(files, list):
                raise ValueError("node transfer manifest files are invalid")
            if any(item.get("path") == value for item in files):
                _replace_record(manifest, replacement)
                updated_nodes.append(node_id)
        if not updated_nodes:
            raise ValueError(f"existing source is absent from all node manifests: {value}")
        refreshed.append({**replacement, "nodes": updated_nodes})

    args.output_plan.parent.mkdir(parents=True, exist_ok=True)
    args.output_plan.write_text(
        json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "protocol": plan.get("protocol"),
        "output_plan": str(args.output_plan),
        "refreshed": refreshed,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
