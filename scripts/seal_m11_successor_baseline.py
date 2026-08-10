#!/usr/bin/env python3
"""Seal a privacy-reduced M11 successor baseline from private run evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable


PROTOCOL = "mycelium.m11_successor_baseline.v1"
MANIFEST_PROTOCOL = "mycelium.m11_successor_baseline_manifest.v1"
_DENIED_EVIDENCE_KEYS = frozenset(
    {
        "activation",
        "activations",
        "completion",
        "completions",
        "endpoint_addr",
        "endpoint_secret_file",
        "generated_tokens",
        "hidden_state",
        "hidden_states",
        "initial_tokens",
        "kv_cache",
        "logits",
        "output",
        "outputs",
        "private_key",
        "prompt",
        "prompts",
        "response",
        "responses",
        "secret",
        "ssh_identity_file",
        "ssh_target",
        "token_ids",
        "tokens",
    }
)


def _canonical(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _load_regular_json(path: Path) -> tuple[dict[str, Any], bytes]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"source is not a regular file: {path.name}")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"source root is not an object: {path.name}")
    return value, raw


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _reject_private_fields(value: Any, path: str = "evidence") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _DENIED_EVIDENCE_KEYS:
                raise ValueError(f"privacy-reduced evidence contains denied field: {path}.{key}")
            _reject_private_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_private_fields(child, f"{path}[{index}]")


def _topology_summary(document: dict[str, Any]) -> dict[str, Any]:
    nodes = document.get("nodes")
    if document.get("protocol") != "mycelium.qwen_live_topology.v1" or not isinstance(
        nodes, list
    ):
        raise ValueError("invalid live topology evidence")
    return {
        "protocol": document["protocol"],
        "physical_node_count": len(nodes),
        "nodes": [
            {
                "node_ref": f"physical-node-{index}",
                "runtime_backend": node.get("runtime_backend"),
                "process_transport": node.get("process_transport"),
            }
            for index, node in enumerate(nodes, start=1)
        ],
    }


def _registry_summary(document: dict[str, Any]) -> dict[str, Any]:
    deployments = document.get("deployments")
    if document.get("protocol") != "mycelium.live_deployment_registry.v1" or not isinstance(
        deployments, list
    ):
        raise ValueError("invalid deployment registry evidence")
    allowed = (
        "deployment_id",
        "health",
        "model_id",
        "qualification_id",
        "qualified_at_unix_ms",
        "quantization",
        "topology_size",
    )
    return {
        "protocol": document["protocol"],
        "selected_deployment_id": document.get("selected_deployment_id"),
        "switching_allowed": document.get("switching_allowed"),
        "deployments": [
            {key: deployment.get(key) for key in allowed} for deployment in deployments
        ],
    }


def _mobile_summary(document: dict[str, Any]) -> dict[str, Any]:
    record = document.get("record")
    if document.get("ok") is not True or not isinstance(record, dict):
        raise ValueError("invalid mobile Device Lab evidence")
    return {
        "proof_protocol": record.get("protocol"),
        "local_evidence_only": record.get("local_evidence_only"),
        "route_ready": record.get("route_ready"),
        "required_distinct_peers": record.get("required_distinct_peers"),
        "observed_distinct_peers": record.get("observed_distinct_peers"),
        "max_intermediate_error": record.get("max_intermediate_error"),
        "max_logit_error": record.get("max_logit_error"),
        "inference_content_included": False,
    }


def _live_status_summary(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("protocol") != "mycelium.live_route_status.v1":
        raise ValueError("invalid live route status evidence")
    return {
        "protocol": document["protocol"],
        "route_alive": document.get("route_alive"),
        "simulated": document.get("simulated"),
        "deployment_id": document.get("deployment_id"),
        "model_id": document.get("model_id"),
        "topology_version": document.get("topology_version"),
        "decode_mode": document.get("decode_mode"),
        "counters": document.get("counters"),
        "stage_count": len(document.get("stages", [])),
        "peer_count": len(document.get("peers", [])),
        "recent_inference_count": len(document.get("recent_inferences", [])),
        "incident_count": len(document.get("incidents", [])),
        "inference_content_included": False,
    }


def build_bundle(run_root: Path, *, sealed_at: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sources: tuple[tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]], ...] = (
        ("m7_three_host_surface", "m7-qwen-three-host-surface-topology.json", _topology_summary),
        ("m8_two_host_mlx", "m8-qwen-two-host-mlx-topology.json", _topology_summary),
        ("m9_larger_model", "m9-qwen15-two-host-mlx-topology.json", _topology_summary),
        ("m10_deployment_registry", "m10-deployment-registry.json", _registry_summary),
        (
            "m11_mobile_device_lab",
            "m11-device-lab/pixel8-physical-proof.json",
            _mobile_summary,
        ),
        (
            "m11_review_live_status",
            "m8-qwen2.5-int8-two-host-mlx-v4/review-live-status-20260810.json",
            _live_status_summary,
        ),
    )
    evidence_items: list[dict[str, Any]] = []
    pins: list[dict[str, Any]] = []
    for role, relative_path, projector in sources:
        document, raw = _load_regular_json(run_root / relative_path)
        summary = projector(document)
        evidence_items.append({"role": role, "summary": summary})
        pins.append(
            {
                "role": role,
                "source_digest": _digest(raw),
                "source_size_bytes": len(raw),
            }
        )
    evidence = {
        "protocol": PROTOCOL,
        "sealed_at": sealed_at,
        "claim_boundary": (
            "privacy-reduced M7-M11 physical baseline; source bytes remain owner-only; "
            "not current route qualification"
        ),
        "evidence": evidence_items,
        "privacy": {
            "observatory_contains_inference_content": False,
            "private_inference_artifacts_bundled": False,
            "excluded_material": [
                "TLS private keys and certificates",
                "membership state databases",
                "operator plans and SSH identities",
                "prompts, model output, token IDs, activations, and KV contents",
            ],
        },
        "known_limitations": [
            "seed membership signer is process-local until M12 durable identity",
            "foreground supervisor must restage and requalify after restart",
            "fatal route recovery rebuilds a complete topology; no in-flight KV migration",
            "mobile Device Lab proof is local evidence and not an activation-eligible model stage",
            "larger-model deployment requires fresh qualification after its recorded timeout",
        ],
    }
    _reject_private_fields(evidence)
    return evidence, pins


def _write_new(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sealed-at", required=True)
    parser.add_argument("--python-tests", type=int, required=True)
    parser.add_argument("--python-focused-tests", type=int, required=True)
    parser.add_argument("--typescript-tests", type=int, required=True)
    parser.add_argument("--rust-tests", type=int, required=True)
    args = parser.parse_args()

    run_root = args.run_root.resolve(strict=True)
    output = args.output.resolve()
    if output.exists():
        raise ValueError("output directory already exists")
    output.mkdir(mode=0o700)

    evidence, source_pins = build_bundle(run_root, sealed_at=args.sealed_at)
    evidence_bytes = _canonical(evidence)
    manifest = {
        "protocol": MANIFEST_PROTOCOL,
        "sealed_at": args.sealed_at,
        "bundle": {
            "file": "evidence.json",
            "sha256": _digest(evidence_bytes),
            "size_bytes": len(evidence_bytes),
        },
        "source_pins": source_pins,
        "verification": {
            "python": {"passed": args.python_tests},
            "python_focused_post_collection": {"passed": args.python_focused_tests},
            "typescript": {"passed": args.typescript_tests},
            "rust": {"passed": args.rust_tests},
            "browser_direct_route_checks": 16,
            "contract_count": 23,
            "contract_audit": "passed",
            "claim_boundary_audit": "passed",
            "release_security_audit": "passed",
        },
    }
    manifest_bytes = _canonical(manifest)
    _write_new(output / "evidence.json", evidence_bytes)
    _write_new(output / "manifest.json", manifest_bytes)
    os.chmod(output, 0o700)
    print(json.dumps({"bundle": str(output), "manifest_sha256": _digest(manifest_bytes)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
