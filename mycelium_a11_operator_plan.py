# SPDX-License-Identifier: AGPL-3.0-only
"""A11 operator-plan extension: stage the owner-authorised draft on the entry node.

Input is an ordinary operator plan produced by ``scripts/build_qwen_live_route.py``
(its ``controller.source_root`` transfer bundle, run plan and transfer
manifests).  :func:`extend_operator_plan` returns a new, *unbound* plan over a
cloned bundle that:

* copies the exact draft snapshot files the loader reads (``config.json``, the
  weight files and the optional weight index) to ``a11/draft/`` after checking
  every weight file against the owner-authorised SHA-256;
* writes the entry node's own ``a11/<entry_node_id>/node.json`` binding (model
  id, resolved commit, weight digests, width and policy);
* sets the run plan's decode mode to ``stage_local_kv`` (A11 needs stage-local
  KV for its VERIFY rounds);
* recomputes the transfer manifest and per-node selections, so the draft and
  its binding ship to the entry node only, content-digested like every other
  staged artifact.

The draft is then verified twice: by staging (every file's content digest in the
transfer manifest) and by the node at configure time (the binding's weight
digests, before any weight is used).  The offers must be re-signed afterwards
(the A7 module's ``bind_extended_plan``, the run-day bind path).  Nothing here
measures, admits or qualifies speculation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_A11_PRIVATE_PREFIX = "a11/"
DRAFT_DIR = "a11/draft"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class A11PlanError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise A11PlanError(code)


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _clone_tree(source: Path, destination: Path) -> None:
    _require(source.is_dir() and not destination.exists(), "a11_plan_bundle_clone_invalid")
    if sys.platform == "darwin":
        # APFS clones: the target tensors cost no extra disk.
        completed = subprocess.run(["cp", "-Rc", str(source), str(destination)], capture_output=True, check=False)
        if completed.returncode == 0:
            return
    shutil.copytree(source, destination)


def _copy_file(source: Path, destination: Path) -> None:
    """Copy one file's bytes (an APFS clone where possible; symlinks are followed)."""

    if sys.platform == "darwin":
        completed = subprocess.run(
            ["cp", "-c", str(source.resolve(strict=True)), str(destination)], capture_output=True, check=False
        )
        if completed.returncode == 0:
            return
    shutil.copyfile(source, destination)


def draft_snapshot_files(snapshot: Path) -> tuple[list[str], list[str]]:
    """``(all files to stage, weight files)`` exactly as the draft loader reads them."""

    _require(snapshot.is_dir() and (snapshot / "config.json").is_file(), "a11_draft_snapshot_invalid")
    index = snapshot / "model.safetensors.index.json"
    if index.is_file():
        try:
            weights = sorted(set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values()))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise A11PlanError("a11_draft_snapshot_invalid") from None
        extra = ["model.safetensors.index.json"]
    else:
        weights = ["model.safetensors"]
        extra = []
    _require(
        bool(weights) and all(isinstance(name, str) and _FILE_NAME.fullmatch(name) for name in weights),
        "a11_draft_snapshot_invalid",
    )
    _require(all((snapshot / name).is_file() for name in weights), "a11_draft_snapshot_invalid")
    return sorted({"config.json", *extra, *weights}), weights


def extend_operator_plan(
    plan: Mapping[str, Any],
    *,
    out_bundle: Path,
    draft_snapshot: Path,
    model_id: str,
    resolved_commit: str,
    expected_sha256: Mapping[str, str],
    width: int = 4,
    quantize_bits: int | None = 8,
    minimum_baseline_samples: int = 16,
    warm_target_tpot_ms: Mapping[str, float] | None = None,
    entry_node_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The unbound A11 plan over a clone of the plan's bundle, plus a summary."""

    from mycelium_a11_node_wiring import PROTOCOL, node_config_path_for, validate_node_config
    from physical_inference_qualification import node_transfer_manifests
    from scripts.build_qwen_live_route import _transfer_manifest, _write_document

    document = json.loads(json.dumps(dict(plan)))
    controller = document["controller"]
    run_plan = controller["run_plan"]
    nodes = {node["node_id"]: node for node in run_plan["nodes"]}
    entry = run_plan["entry_node_id"] if entry_node_id is None else entry_node_id
    _require(entry == run_plan["entry_node_id"] and entry in nodes, "a11_plan_entry_invalid")
    _require(
        isinstance(expected_sha256, Mapping)
        and bool(expected_sha256)
        and all(isinstance(v, str) and _HEX64.fullmatch(v) for v in expected_sha256.values()),
        "a11_plan_expected_sha256_invalid",
    )
    source_bundle = Path(controller["source_root"])
    _require(
        not any(
            record["path"].startswith(_A11_PRIVATE_PREFIX) for record in controller["transfer_manifest"]["files"]
        ),
        "a11_plan_already_extended",
    )
    snapshot = Path(draft_snapshot)
    files, weights = draft_snapshot_files(snapshot)
    # Owner authorisation first: the exact weight files, no more and no less.
    _require(set(expected_sha256) == set(weights), "a11_draft_weight_set_mismatch")
    for name in weights:
        _require(_sha256_file(snapshot / name) == expected_sha256[name], "a11_draft_digest_mismatch")

    binding = validate_node_config(
        {
            "protocol": PROTOCOL,
            "deployment_id": run_plan["deployment_id"],
            "node_id": entry,
            "draft": {
                "model_dir": DRAFT_DIR,
                "model_id": model_id,
                "resolved_commit": resolved_commit,
                "expected_sha256": dict(expected_sha256),
                "quantize_bits": quantize_bits,
            },
            "policy": {
                "width": width,
                "warm_target_tpot_ms": None if warm_target_tpot_ms is None else dict(warm_target_tpot_ms),
                "minimum_baseline_samples": minimum_baseline_samples,
            },
        },
        deployment_id=run_plan["deployment_id"],
        node_id=entry,
    )

    _clone_tree(source_bundle, out_bundle)
    bundle = out_bundle
    _require(not (bundle / "a11").exists(), "a11_plan_bundle_has_foreign_a11_files")
    draft_root = bundle / DRAFT_DIR
    draft_root.mkdir(parents=True, mode=0o700)
    for name in files:
        _copy_file(snapshot / name, draft_root / name)
    # The staged copy is what ships; re-check it, not only the source.
    for name in weights:
        _require(_sha256_file(draft_root / name) == expected_sha256[name], "a11_draft_digest_mismatch")
    config_path = node_config_path_for(entry)
    _write_document(bundle / config_path, binding)
    run_plan["decode_mode"] = "stage_local_kv"

    owned = {config_path, *(f"{DRAFT_DIR}/{name}" for name in files)}
    transfer = _transfer_manifest(bundle)
    unexpected = sorted(
        record["path"]
        for record in transfer["files"]
        if record["path"].startswith(_A11_PRIVATE_PREFIX) and record["path"] not in owned
    )
    _require(not unexpected, "a11_plan_bundle_has_foreign_a11_files")
    selections: dict[str, list[str]] = {}
    for node_id, manifest_document in controller["node_transfer_manifests"]["manifests"].items():
        chosen = {record["path"] for record in manifest_document["files"]}
        if node_id == entry:
            chosen |= owned
        selections[node_id] = sorted(chosen)
    controller["transfer_manifest"] = transfer
    controller["node_transfer_manifests"] = node_transfer_manifests(transfer, selections)
    controller["source_root"] = str(bundle)
    document["plan_id"] = f"{document['plan_id']}-a11"
    by_path = {record["path"]: record for record in transfer["files"]}
    summary = {
        "protocol": "mycelium.a11_operator_plan_extension.v1",
        "entry_node_id": entry,
        "node_config_path": config_path,
        "draft": {
            "model_id": model_id,
            "resolved_commit": resolved_commit,
            "model_dir": DRAFT_DIR,
            "files": {f"{DRAFT_DIR}/{name}": copy.deepcopy(by_path[f"{DRAFT_DIR}/{name}"]) for name in files},
            "expected_sha256": dict(expected_sha256),
            "quantize_bits": quantize_bits,
        },
        "policy": binding["policy"],
        "decode_mode": "stage_local_kv",
        "binding_content_digest": by_path[config_path]["content_digest"],
    }
    return document, summary


__all__ = ("A11PlanError", "DRAFT_DIR", "draft_snapshot_files", "extend_operator_plan")
