#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Derive a disjoint-path A6 operator plan from a bound two- or three-host plan.

A6 physical recovery needs a successor path that survives the loss of every
source placement (spec 2026-08-18 section 10: "two compatible independently
qualified immutable physical paths"). The production controller runs exactly
one node process per physically distinct host and one ACTIVE placement per
node process, so with two hosts the only disjoint two-path shape is a
complete-model replica on each host: one stage, two placements, each a full
path (``placement-000`` on the entry host, ``placement-001`` on the other).

This derivation reuses the canonical builder steps of
``scripts/build_qwen_live_route.py`` (local compile, provision, stage pack,
load; target report and proof rebound to each node's own artifact root) and
the plan's own representation, runtime backends, control-plane binding,
peers, startup prompt and run id. Nothing is invented:

* each node's full assignment is compiled by ``compile_layer_assignments`` for
  that node's existing runtime and a PROVEN artifact root: the root on the
  node that holds every model shard. A node whose plan root is the
  controller's own base artifact root is proven on disk (every pack artifact
  present with its manifest size); any other node needs the operator's
  assertion ``--artifact-root NODE=PATH``, otherwise the derivation refuses
  (``a6_replica_artifact_root_unproven``). The base plan's root of a remote
  node was provisioned for the old split and may lack the other stage's shard,
  and the derive-time challenge cannot see that: it measures the controller's
  local copy;
* each load proof is measured by loading the full assignment locally with that
  node's runtime backend, exactly as the builder measured the split proofs,
  then its ``runtime_identity`` (backend version and device) is rebound to the
  identity OBSERVED on that node's own interpreter: the node recomputes the
  proof at configure and refuses ``placement_load_proof_mismatch`` if, say,
  its NumPy differs from the deriving interpreter's. A node with local process
  transport is observed here by running its own ``python_executable``; any
  other node needs ``--runtime-identity NODE=PATH`` (the JSON printed by
  ``RUNTIME_IDENTITY_PROGRAM`` run with that node's interpreter on that node),
  otherwise the derivation refuses (``a6_replica_runtime_identity_unobserved``);
* the startup challenge output is recomputed through BOTH full paths and the
  derivation refuses unless the two backends agree token for token (the A6
  successor qualification replays the same challenge on the successor path);
* offers keep their recipients and are re-signed by the plan binding with the
  seed's own key (``--seed-state-root``), or left for the caller to bind.

Offline: no peer, transport or fleet contact. Staging the derived bundle
(``stage_live_route``) transfers each node its ``node_transfer_manifests``
selection, which for a complete-model replica is the control documents, the
code AND every model shard (about 1 GB per node for the 0.5B model), into that
node's staging root, unless ``--preposition-artifacts`` binds every shard to
the copy already in that node's proven root (``controller.prepositioned_artifacts``).
With a third host (``placement-002``) the plan has three disjoint full paths. The node, however, loads from the assignment's artifact
root, so a remote node's asserted root must itself hold the complete model
before the plan is served (the node refuses ``invalid_stage_pack_file`` at
configure otherwise).
"""
from __future__ import annotations

import argparse
import copy
import gc
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROTOCOL = "mycelium.a6_replica_plan_derivation.v1"
#: One complete-model placement per host, in plan node order (entry first). A6
#: needs at least two hosts; a third host gives the exhausted-attempts case a
#: real second disjoint successor.
PLACEMENT_IDS = ("placement-000", "placement-001", "placement-002")
MINIMUM_HOSTS = 2
PREPOSITIONED_PROTOCOL = "mycelium.controller_prepositioned_artifacts.v1"
REPLICA_GROUP = "full-model"
FULL_COMPONENTS = ("input_embedding", "decoder", "final_norm", "lm_head")
RUNTIME_IDENTITY_FIELDS = ("backend", "backend_version", "device")
# Mirrors runtime_loader._actual_runtime_identity for the host-dependent fields.
# Run on the node with that node's own interpreter: ``python -c PROGRAM BACKEND``.
RUNTIME_IDENTITY_PROGRAM = """
import importlib.metadata as metadata, json, sys
backend = sys.argv[1]
if backend == "numpy":
    device = "cpu"
elif backend == "mlx":
    import mlx.core as mx
    device = str(mx.default_device())
else:
    raise SystemExit("unsupported backend")
print(json.dumps({"backend": backend, "backend_version": metadata.version(backend),
                  "device": device}, sort_keys=True))
"""


class DerivationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _single_node_route(manifest: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    from model_manifest import manifest_digest_ref

    layers = int(manifest["num_layers"])
    full = {"start_layer": 0, "end_layer_exclusive": layers, "layer_count": layers}
    return {
        "ok": True,
        "protocol": "mycelium.manual_provisioning_route.v1",
        "claim_boundary": "a6 complete-model replica assignment compilation only",
        "model": {
            "model_id": manifest["model_id"],
            "num_layers": layers,
            "manifest_digest": manifest_digest_ref(manifest),
            "resolved_commit": manifest["resolved_commit"],
        },
        "route": [{"node_id": node_id, "range": full}],
        "node_order": [node_id],
    }


def _compile_full(
    manifest: Mapping[str, Any], template: Mapping[str, Any], cache_root: str
) -> dict[str, Any]:
    from layer_assignment import compile_layer_assignments

    node_id = template["node_id"]
    (assignment,) = compile_layer_assignments(
        route_plan=_single_node_route(manifest, node_id),
        manifest=dict(manifest),
        deployment_id=template["deployment_id"],
        deployment_epoch=template["deployment_epoch"],
        cache_roots={node_id: cache_root},
        runtime_by_node={node_id: {
            field: template["runtime"][field] for field in ("backend", "dtype", "quantization")}},
        control_plane_binding=copy.deepcopy(template["control_plane_binding"]),
    )
    if tuple(assignment["components"]) != FULL_COMPONENTS:
        raise DerivationError("a6_replica_assignment_not_complete")
    return assignment


def _check_local_root(manifest: Mapping[str, Any], template: Mapping[str, Any], root: str) -> None:
    """Every artifact the full pack names exists under ``root`` with its size."""

    base = Path(root)
    for record in _compile_full(manifest, template, root)["files"]:
        try:
            metadata = (base / record["path"]).lstat()
        except OSError:
            raise DerivationError("a6_replica_local_artifact_missing") from None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != record["size_bytes"]:
            raise DerivationError("a6_replica_local_artifact_missing")


def resolve_artifact_roots(
    manifest: Mapping[str, Any],
    templates: Sequence[Mapping[str, Any]],
    asserted: Mapping[str, str],
    local_artifact_root: Path,
) -> dict[str, dict[str, str]]:
    """Return ``{node_id: {"artifact_root", "proof"}}`` or refuse.

    ``proof`` is ``local_disk_checked`` when the root is the controller's own
    base artifact root (checked here, file by file) and ``operator_asserted``
    for a root the operator vouched for with ``--artifact-root``. A node with
    neither has no proven root and the derivation refuses.
    """

    node_ids = [template["node_id"] for template in templates]
    unknown = sorted(set(asserted) - set(node_ids))
    if unknown:
        raise DerivationError("a6_replica_artifact_root_unknown_node")
    local = Path(local_artifact_root).resolve()
    roots: dict[str, dict[str, str]] = {}
    for template in templates:
        node_id = template["node_id"]
        root = asserted.get(node_id, template["artifact_cache_root"])
        if (not isinstance(root, str) or not os.path.isabs(root)
                or os.path.normpath(root) != root or root == os.sep):
            raise DerivationError("a6_replica_artifact_root_invalid")
        if Path(root).resolve() == local:
            _check_local_root(manifest, template, root)
            roots[node_id] = {"artifact_root": root, "proof": "local_disk_checked"}
        elif node_id in asserted:
            roots[node_id] = {"artifact_root": root, "proof": "operator_asserted"}
        else:
            raise DerivationError("a6_replica_artifact_root_unproven")
    return roots


def _valid_runtime_identity(observed: Any, backend: str) -> dict[str, str]:
    if (not isinstance(observed, Mapping) or set(observed) != set(RUNTIME_IDENTITY_FIELDS)
            or not all(isinstance(observed[field], str) and observed[field]
                       for field in RUNTIME_IDENTITY_FIELDS)
            or observed["backend_version"] == "unknown"):
        raise DerivationError("a6_replica_runtime_identity_invalid")
    if observed["backend"] != backend:
        raise DerivationError("a6_replica_runtime_identity_backend_mismatch")
    return {field: observed[field] for field in RUNTIME_IDENTITY_FIELDS}


def observe_local_runtime_identity(python_executable: str, backend: str) -> dict[str, str]:
    """Run ``RUNTIME_IDENTITY_PROGRAM`` with a node's own local interpreter."""

    try:
        completed = subprocess.run(
            [python_executable, "-c", RUNTIME_IDENTITY_PROGRAM, backend],
            capture_output=True, text=True, timeout=60.0, check=False)
        observed = json.loads(completed.stdout) if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        observed = None
    if observed is None:
        raise DerivationError("a6_replica_runtime_identity_unobserved")
    return _valid_runtime_identity(observed, backend)


def resolve_runtime_identities(
    controller: Mapping[str, Any],
    templates: Sequence[Mapping[str, Any]],
    observed: Mapping[str, Any],
    *,
    observe_local: Callable[[str, str], dict[str, str]] = observe_local_runtime_identity,
) -> dict[str, dict[str, Any]]:
    """Return ``{node_id: {"identity", "source"}}`` or refuse.

    A load proof binds the backend version and device of the interpreter that
    loads it. The node recomputes its proof with its OWN interpreter, so the
    identity is taken from that interpreter, never from the deriving one:
    ``operator_observed`` for an identity the operator measured on the node
    (``--runtime-identity``), ``local_interpreter_observed`` for a node with
    local process transport whose ``python_executable`` is run here.
    """

    node_ids = [template["node_id"] for template in templates]
    if set(observed) - set(node_ids):
        raise DerivationError("a6_replica_runtime_identity_unknown_node")
    peers = {peer.get("node_id"): peer for peer in controller.get("peers", ())}
    executables = {
        node.get("node_id"): node.get("python_executable")
        for node in controller["run_plan"]["nodes"]
    }
    identities: dict[str, dict[str, Any]] = {}
    for template in templates:
        node_id = template["node_id"]
        backend = template["runtime"]["backend"]
        python_executable = executables.get(node_id)
        if node_id in observed:
            identities[node_id] = {
                "identity": _valid_runtime_identity(observed[node_id], backend),
                "source": "operator_observed",
            }
        elif (peers.get(node_id, {}).get("process_transport") == "local"
              and isinstance(python_executable, str) and os.path.isabs(python_executable)):
            identities[node_id] = {
                "identity": _valid_runtime_identity(
                    observe_local(python_executable, backend), backend),
                "source": "local_interpreter_observed",
            }
        else:
            raise DerivationError("a6_replica_runtime_identity_unobserved")
    return identities


def _rebind_runtime_identity(proof: dict[str, Any], identity: Mapping[str, str]) -> None:
    """Replace the deriving interpreter's host fields with the node's own."""

    measured = proof.get("runtime_identity")
    if not isinstance(measured, dict) or measured.get("backend") != identity["backend"]:
        raise DerivationError("a6_replica_runtime_identity_backend_mismatch")
    measured["backend_version"] = identity["backend_version"]
    measured["device"] = identity["device"]


def _run_challenge(loaded: Any, backend: str, prompt: Sequence[int], count: int) -> tuple[int, ...]:
    from mycelium_router.decoding import quantized_greedy_token_id

    context = tuple(int(token) for token in prompt)
    generated: list[int] = []
    for _ in range(count):
        if backend == "mlx":
            import mlx.core as mx
            from runtime_loader import execute_loaded_stage

            logits = execute_loaded_stage(loaded, token_ids=mx.array((context,), dtype=mx.int32))
            mx.eval(logits)
        elif backend == "numpy":
            import numpy as np
            from runtime_loader import execute_loaded_numpy_stage

            logits = execute_loaded_numpy_stage(
                loaded, token_ids=np.asarray((context,), dtype=np.int64))
        else:
            raise DerivationError("a6_replica_runtime_backend_unsupported")
        token = quantized_greedy_token_id(logits[0, -1, :].tolist())
        generated.append(token)
        context = (*context, token)
    return tuple(generated)


def _release_memory() -> None:
    gc.collect()
    try:
        import mlx.core as mx

        clear = getattr(mx, "clear_cache", None)
        if callable(clear):
            clear()
    except ImportError:
        pass


def replica_documents(
    *,
    manifest: Mapping[str, Any],
    templates: Sequence[Mapping[str, Any]],
    local_artifact_root: Path,
    load_generation: int,
    challenge_prompt: Sequence[int],
    challenge_count: int,
    runtime_identities: Mapping[str, Mapping[str, str]],
    artifact_roots: Mapping[str, str] | None = None,
    on_progress: Callable[[str, Mapping[str, Any]], None] = lambda _stage, _fields: None,
) -> dict[str, Any]:
    """Compile, measure and bind one complete-model placement per node.

    ``templates`` are the nodes' current assignments, in placement order (the
    first is the entry/primary path). ``artifact_roots`` maps each node to its
    proven artifact root (see ``resolve_artifact_roots``); a node missing from
    it keeps its template root. ``runtime_identities`` maps every node to the
    runtime identity observed on that node (see ``resolve_runtime_identities``);
    a node missing from it refuses. Returns assignments, stage packs,
    verifications, proofs, the replicated execution graph and the measured
    challenge output of every path.
    """

    from mycelium_qualification.physical_deployment import build_replicated_execution_graph
    from runtime_loader import canonical_json, load_assignment_stage
    from scripts.build_qwen_live_route import _target_proof, _target_report
    from stage_pack import artifact_report_for_loader, compile_stage_pack, verify_stage_pack
    from weight_provisioning import provision_assignment

    if not MINIMUM_HOSTS <= len(templates) <= len(PLACEMENT_IDS):
        raise DerivationError("a6_replica_requires_two_hosts")
    placement_ids = PLACEMENT_IDS[:len(templates)]
    local_root = Path(local_artifact_root)

    def local_fetch(model_id, revision, filename, cache_root, *, local_files_only):
        del model_id, revision, cache_root, local_files_only
        return local_root / filename, True

    assignments, packs, verifications, proofs, outputs = [], [], [], [], {}
    if set(runtime_identities) != {template["node_id"] for template in templates}:
        raise DerivationError("a6_replica_runtime_identity_unobserved")
    for template in templates:
        node_id = template["node_id"]
        backend = template["runtime"]["backend"]
        identity = _valid_runtime_identity(runtime_identities[node_id], backend)
        target_root = (artifact_roots or {}).get(node_id, template["artifact_cache_root"])
        assignment = _compile_full(manifest, template, target_root)
        local = _compile_full(manifest, template, str(local_root))
        report = provision_assignment(local, fetch_file=local_fetch, local_files_only=True)
        local_pack = compile_stage_pack(local, dict(manifest), report)
        local_verification = verify_stage_pack(local_pack, assignment=local, manifest=dict(manifest))
        loader_report = artifact_report_for_loader(
            local_pack, local_verification, assignment=local, manifest=dict(manifest))
        on_progress("loading", {"node_id": node_id, "backend": backend})
        loaded = load_assignment_stage(local, loader_report, load_generation=load_generation)
        try:
            base_proof = json.loads(canonical_json(loaded.proof))
            _rebind_runtime_identity(base_proof, identity)
            outputs[node_id] = list(
                _run_challenge(loaded, backend, challenge_prompt, challenge_count))
        finally:
            del loaded
            _release_memory()
        on_progress("challenged", {"node_id": node_id, "output": outputs[node_id]})
        target_report = _target_report(loader_report, assignment)
        pack = compile_stage_pack(
            assignment, dict(manifest), target_report, source_artifact_root=local_root)
        verification = verify_stage_pack(
            pack, assignment=assignment, manifest=dict(manifest), source_artifact_root=local_root)
        assignments.append(assignment)
        packs.append(pack)
        verifications.append(verification)
        proofs.append(_target_proof(base_proof, assignment, pack, verification))

    layers = int(manifest["num_layers"])
    route_plan = {
        "protocol": "mycelium.route_plan.v2",
        "placements": [
            {
                "placement_id": placement_id,
                "replica_group_id": REPLICA_GROUP,
                "node_id": assignment["node_id"],
                "layer_range": {"start": 0, "end": layers},
                "primary": index == 0,
            }
            for index, (placement_id, assignment) in enumerate(
                zip(placement_ids, assignments, strict=True))
        ],
        "forward_edges": [],
        "loopbacks": [],
    }
    graph = build_replicated_execution_graph(
        dict(zip(placement_ids, assignments, strict=True)),
        dict(zip(placement_ids, proofs, strict=True)),
        route_plan,
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    return {
        "assignments": assignments,
        "packs": packs,
        "verifications": verifications,
        "proofs": proofs,
        "graph": graph,
        "challenge_outputs": outputs,
    }


def prepositioned_artifacts(
    transfer: Mapping[str, Any],
    packs: Sequence[Mapping[str, Any]],
    roots: Mapping[str, Mapping[str, str]],
    node_manifests: dict[str, Any],
) -> dict[str, Any]:
    """Bind each node's model shards to the copies already in its proven root.

    Staging then copies every shard on the node itself from ``source_path``
    (verifying size and content digest against the controller's transfer
    record) instead of shipping it again; each shard is removed from that
    node's transfer selection, which the controller requires to be disjoint.
    """

    base = {record["path"]: record for record in transfer["files"]}
    members: dict[str, list[dict[str, Any]]] = {}
    for pack in packs:
        node_id = pack["node_id"]
        root = roots[node_id]["artifact_root"]
        records = []
        for artifact in pack["artifacts"]:
            destination = f"deployment/{artifact['upstream_path']}"
            record = base.get(destination)
            relative = PurePosixPath(str(artifact.get("relative_path")))
            if (record is None or relative.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or record["size_bytes"] != artifact.get("size_bytes")):
                raise DerivationError("a6_replica_preposition_unbound")
            records.append({
                "destination_path": destination,
                "source_path": str(PurePosixPath(root) / relative),
                "size_bytes": record["size_bytes"],
                "content_digest": record["content_digest"],
            })
        if not records or node_id in members:
            raise DerivationError("a6_replica_preposition_unbound")
        records.sort(key=lambda item: item["destination_path"])
        members[node_id] = records
        destinations = {item["destination_path"] for item in records}
        manifest = node_manifests["manifests"][node_id]
        manifest["files"] = [item for item in manifest["files"]
                             if item["path"] not in destinations]
    if set(members) != set(node_manifests["manifests"]):
        raise DerivationError("a6_replica_preposition_unbound")
    return {"protocol": PREPOSITIONED_PROTOCOL, "members": members}


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy an immutable bundle; new documents are written beside it.

    Staging refuses any transfer file with st_nlink != 1, so the copy must be
    bytes, never hard links (a link would also make the source unstageable).
    """

    if destination.exists():
        raise DerivationError("a6_replica_bundle_exists")
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    destination.chmod(0o700)


def derive_plan(
    base: Mapping[str, Any],
    *,
    bundle_out: Path,
    artifact_roots: Mapping[str, str] | None = None,
    runtime_identities: Mapping[str, Any] | None = None,
    observe_local: Callable[[str, str], dict[str, str]] = observe_local_runtime_identity,
    on_progress: Callable[[str, Mapping[str, Any]], None] = lambda _stage, _fields: None,
    preposition: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (unbound derived plan, derivation report); writes ``bundle_out``.

    ``artifact_roots`` holds the operator's assertions ``{node_id: root}``: the
    root on that node which holds the complete model. Every node not proven
    local needs one (``resolve_artifact_roots``). ``runtime_identities`` holds
    the runtime identities ``{node_id: {"backend", "backend_version",
    "device"}}`` the operator observed on the nodes; every node without local
    process transport needs one (``resolve_runtime_identities``).
    """

    from mycelium_qualification.physical_deployment import build_physical_device_states
    from scripts.build_qwen_live_route import (
        _bytes,
        _digest,
        _node_transfer_manifests,
        _physical_graph_document,
        _transfer_manifest,
        _write_document,
    )

    if Path(bundle_out).exists():
        raise DerivationError("a6_replica_bundle_exists")
    plan = json.loads(json.dumps(dict(base)))
    controller = plan["controller"]
    run_plan = controller["run_plan"]
    bundle_in = Path(controller["source_root"])
    nodes = run_plan["nodes"]
    if (not MINIMUM_HOSTS <= len(nodes) <= len(PLACEMENT_IDS)
            or nodes[0]["node_id"] != run_plan["entry_node_id"]):
        raise DerivationError("a6_replica_requires_entry_first_two_hosts")
    load_generations = {node["configure"]["load_generation"] for node in nodes}
    if len(load_generations) != 1:
        raise DerivationError("a6_replica_load_generation_ambiguous")
    (load_generation,) = load_generations
    manifest_files = {node["configure"]["manifest_file"] for node in nodes}
    if len(manifest_files) != 1:
        raise DerivationError("a6_replica_manifest_ambiguous")
    (manifest_file,) = manifest_files
    manifest = _read_json(bundle_in / manifest_file)
    templates = [_read_json(bundle_in / node["configure"]["assignment_file"]) for node in nodes]
    if [template["node_id"] for template in templates] != [node["node_id"] for node in nodes]:
        raise DerivationError("a6_replica_template_node_mismatch")
    expected = list(run_plan["expected_token_ids"])
    local_artifact_root = bundle_in / "deployment"
    roots = resolve_artifact_roots(
        manifest, templates, dict(artifact_roots or {}), local_artifact_root)
    identities = resolve_runtime_identities(
        controller, templates, dict(runtime_identities or {}), observe_local=observe_local)
    documents = replica_documents(
        manifest=manifest,
        templates=templates,
        local_artifact_root=local_artifact_root,
        runtime_identities={node_id: entry["identity"] for node_id, entry in identities.items()},
        artifact_roots={node_id: entry["artifact_root"] for node_id, entry in roots.items()},
        load_generation=load_generation,
        challenge_prompt=run_plan["request"]["prompt_token_ids"],
        challenge_count=len(expected),
        on_progress=on_progress,
    )
    outputs = documents["challenge_outputs"]
    if len({tuple(value) for value in outputs.values()}) != 1:
        # The successor's qualification replays the startup challenge on its
        # own path; backends that disagree can never qualify each other.
        raise DerivationError("a6_replica_backend_parity_mismatch")
    path_output = next(iter(outputs.values()))

    graph_document = _physical_graph_document(documents["graph"])
    states = build_physical_device_states(documents["graph"])
    state_document = json.loads(json.dumps({key: asdict(value) for key, value in states.items()}))

    _copy_tree(bundle_in, bundle_out)
    control = bundle_out / "control"
    files: dict[str, dict[str, str]] = {}
    for node, assignment, pack, verification, proof in zip(
        nodes, documents["assignments"], documents["packs"], documents["verifications"],
        documents["proofs"], strict=True,
    ):
        node_id = node["node_id"]
        names = {
            "assignment_file": f"control/a6r-{node_id}-assignment.json",
            "stage_pack_file": f"control/a6r-{node_id}-stage-pack.json",
        }
        _write_document(bundle_out / names["assignment_file"], assignment)
        _write_document(bundle_out / names["stage_pack_file"], pack)
        _write_document(control / f"a6r-{node_id}-stage-pack-verification.json", verification)
        _write_document(control / f"a6r-{node_id}-load-proof.json", proof)
        files[node_id] = names
    _write_document(control / "a6r-execution-graph.json", graph_document)
    _write_document(control / "a6r-device-states.json", state_document)

    for node in nodes:
        configure = node["configure"]
        configure["assignment_file"] = files[node["node_id"]]["assignment_file"]
        configure["stage_pack_file"] = files[node["node_id"]]["stage_pack_file"]
        configure["graph"] = graph_document
        configure["device_states"] = state_document
    run_plan["expected_token_ids"] = list(path_output)
    transfer = _transfer_manifest(bundle_out)
    controller["source_root"] = str(bundle_out)
    controller["transfer_manifest"] = transfer
    controller["node_transfer_manifests"] = _node_transfer_manifests(transfer, documents["packs"])
    if preposition:
        controller["prepositioned_artifacts"] = prepositioned_artifacts(
            transfer, documents["packs"], roots, controller["node_transfer_manifests"])

    by_node = {
        assignment["node_id"]: (assignment, pack)
        for assignment, pack in zip(documents["assignments"], documents["packs"], strict=True)
    }
    graph_digest = _digest(_bytes(graph_document))
    for envelope in controller["membership_snapshot"]["assignment_offers"]:
        message = envelope["message"]
        assignment, pack = by_node[message["recipient_node_id"]]
        message["assignment_id"] = assignment["assignment_id"]
        message["assignment_digest"] = _digest(_bytes(assignment))
        message["stage_pack_digest"] = pack["stage_pack_digest"]
        message["graph_digest"] = graph_digest
        message["load_generation"] = load_generation
    plan["plan_id"] = f"{plan['plan_id']}-a6-replica"

    report = {
        "protocol": PROTOCOL,
        "base_plan_id": base["plan_id"],
        "plan_id": plan["plan_id"],
        "run_id": plan["run_id"],
        "source_root": str(bundle_out),
        "placements": [
            {
                "placement_id": placement.placement_id,
                "node_id": placement.node_id,
                "assignment_id": placement.assignment_id,
                "runtime_backend": placement.runtime_backend,
                "load_proof_digest": placement.load_proof_digest,
            }
            for stage in documents["graph"].stages
            for placement in stage.placements
        ],
        "artifact_roots": roots,
        "runtime_identities": identities,
        "challenge_outputs": outputs,
        "base_expected_token_ids": expected,
        "expected_token_ids": list(path_output),
        "graph_digest": graph_digest,
        "node_transfer_file_counts": {
            node_id: len(document["files"])
            for node_id, document in controller["node_transfer_manifests"]["manifests"].items()
        },
        "prepositioned_file_counts": (
            None if "prepositioned_artifacts" not in controller else {
                node_id: len(records) for node_id, records
                in controller["prepositioned_artifacts"]["members"].items()}),
        "bound": False,
    }
    return plan, report


def bind(plan: Mapping[str, Any], seed_state_root: Path, *, now: float) -> dict[str, Any]:
    from mycelium_seed.plan_binding import bind_operator_plan_document
    from scripts.bind_operator_plan_to_seed import _load_seed

    signer, swarm_id, seed_node_id, members = _load_seed(Path(seed_state_root))
    bound = bind_operator_plan_document(
        plan, signer=signer, swarm_id=swarm_id, seed_node_id=seed_node_id,
        members=members, now=now)
    bound["now_unix_ms"] = int(now * 1_000)
    return bound


def validate(plan_path: Path) -> dict[str, Any]:
    """Offline structural validation through the product's own loaders."""

    from mycelium_live.representation import native_deployment_projection
    from mycelium_physical_runner import load_operator_plan
    from mycelium_router.serialization import execution_graph_from_dict
    from mycelium_router.validation import validate_execution_graph

    load_operator_plan(plan_path)
    projection = native_deployment_projection(plan_path)
    plan = _read_json(plan_path)
    graph_document = plan["controller"]["run_plan"]["nodes"][0]["configure"]["graph"]
    normalized = json.loads(json.dumps(graph_document))
    for stage in normalized["stages"]:
        stage["range"] = stage.pop("layer_range")
    graph = execution_graph_from_dict(normalized)
    validate_execution_graph(graph)
    placements = [
        (placement.placement_id, placement.node_id)
        for stage in graph.stages for placement in stage.placements
    ]
    hosts = {node for _, node in placements}
    if (len(graph.stages) != 1 or len(hosts) != len(placements)
            or not MINIMUM_HOSTS <= len(hosts) <= len(PLACEMENT_IDS)):
        raise DerivationError("a6_replica_graph_not_two_disjoint_paths")
    return {
        "deployment_id": projection.deployment_id,
        "model_id": projection.model_id,
        "stages": len(graph.stages),
        "placements": placements,
    }


def _write_private(path: Path, document: Mapping[str, Any]) -> None:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-plan", type=Path, required=True)
    parser.add_argument("--output-plan", type=Path, required=True)
    parser.add_argument("--bundle-out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--artifact-root", action="append", default=[], metavar="NODE=PATH",
        help="assert that PATH on NODE holds the complete model (repeatable); required "
             "for every node whose root is not the controller's own base artifact root")
    parser.add_argument(
        "--runtime-identity", action="append", default=[], metavar="NODE=PATH",
        help="JSON runtime identity observed on NODE by running RUNTIME_IDENTITY_PROGRAM with "
             "that node's own python_executable (repeatable); required for every node "
             "without local process transport")
    parser.add_argument(
        "--preposition-artifacts", action="store_true",
        help="stage each node's model shards from its proven artifact root on that node "
             "(controller.prepositioned_artifacts) instead of transferring them")
    binding = parser.add_mutually_exclusive_group(required=True)
    binding.add_argument("--seed-state-root", type=Path)
    binding.add_argument("--unbound", action="store_true",
                         help="leave offers unsigned (structural rehearsal only)")
    args = parser.parse_args(argv)
    for path in (args.output_plan, args.report):
        if path.exists():
            print(json.dumps({"refused": "output_exists", "path": str(path)}))
            return 2

    asserted: dict[str, str] = {}
    for item in args.artifact_root:
        node_id, separator, root = item.partition("=")
        if not separator or not node_id or not root or node_id in asserted:
            print(json.dumps({"refused": "a6_replica_artifact_root_invalid", "value": item}))
            return 2
        asserted[node_id] = root

    observed: dict[str, Any] = {}
    for item in args.runtime_identity:
        node_id, separator, path = item.partition("=")
        if not separator or not node_id or not path or node_id in observed:
            print(json.dumps({"refused": "a6_replica_runtime_identity_invalid", "value": item}))
            return 2
        try:
            observed[node_id] = _read_json(Path(path))
        except (OSError, ValueError):
            print(json.dumps({"refused": "a6_replica_runtime_identity_invalid", "value": item}))
            return 2

    def progress(stage: str, fields: Mapping[str, Any]) -> None:
        print(json.dumps({"stage": stage, **fields}, sort_keys=True), flush=True)

    try:
        plan, report = derive_plan(
            _read_json(args.base_plan), bundle_out=args.bundle_out,
            artifact_roots=asserted, runtime_identities=observed, on_progress=progress,
            preposition=args.preposition_artifacts)
        if args.seed_state_root is not None:
            plan = bind(plan, args.seed_state_root, now=time.time())
            report["bound"] = True
        _write_private(args.output_plan, plan)
        report["validation"] = validate(args.output_plan)
        _write_private(args.report, report)
    except DerivationError as error:
        print(json.dumps({"refused": error.code}))
        return 1
    print(json.dumps({"stage": "derived", "output_plan": str(args.output_plan),
                      "bound": report["bound"], "expected_token_ids": report["expected_token_ids"],
                      "challenge_outputs": report["challenge_outputs"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
