#!/usr/bin/env python3
"""Probe a live target route and publish the closed M20 promotion decision."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import statistics
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_m20_speculation import (  # noqa: E402
    build_speculative_plan,
    build_speculative_runtime,
)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"document_invalid:{path.name}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _fetch(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("live_status_invalid")
    return value


def _fetch_qualification(base_url: str) -> dict[str, Any]:
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    bootstrap = json.load(opener.open(base_url.rstrip("/") + "/api/v1/bootstrap", timeout=15))
    session = bootstrap["session"]
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/qualification/current",
        headers={session["csrf_header"]: session["csrf_token"]},
    )
    value = json.load(opener.open(request, timeout=15))
    if not isinstance(value, dict):
        raise ValueError("qualification_invalid")
    return value


def _model_identity(root: Path, model_id: str, revision: str) -> dict[str, Any]:
    required = (
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
        "model.safetensors",
    )
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise ValueError("local_model_incomplete:" + ",".join(missing))
    config = _read(root / "config.json")
    tokenizer_config = _read(root / "tokenizer_config.json")
    position = {
        key: config.get(key)
        for key in (
            "model_type",
            "max_position_embeddings",
            "rope_theta",
            "sliding_window",
            "use_sliding_window",
        )
    }
    kv_schema = {
        key: config.get(key)
        for key in (
            "hidden_size",
            "num_hidden_layers",
            "num_attention_heads",
            "num_key_value_heads",
            "head_dim",
        )
    }
    special = {
        key: tokenizer_config.get(key)
        for key in (
            "bos_token",
            "eos_token",
            "pad_token",
            "unk_token",
            "added_tokens_decoder",
        )
    }
    return {
        "model_id": model_id,
        "model_revision": revision,
        "tokenizer_digest": _sha256(root / "tokenizer.json"),
        "vocabulary_size": int(config["vocab_size"]),
        "special_tokens_digest": _json_digest(special),
        "position_semantics_digest": _json_digest(position),
        "kv_schema_digest": _json_digest(kv_schema),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-dir", type=Path, required=True)
    parser.add_argument("--target-model-root", type=Path, required=True)
    parser.add_argument("--target-model-id", required=True)
    parser.add_argument("--target-revision", required=True)
    parser.add_argument("--draft-model-root", type=Path, required=True)
    parser.add_argument("--draft-model-id", required=True)
    parser.add_argument("--draft-revision", required=True)
    parser.add_argument("--live-base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    liveness = _fetch(args.live_base_url.rstrip("/") + "/__mycelium/m19-liveness")
    recovery = _fetch(args.live_base_url.rstrip("/") + "/__mycelium/m19-recovery-plan")
    live = _fetch(args.live_base_url.rstrip("/") + "/__mycelium/live-status")
    qualification = _fetch_qualification(args.live_base_url)
    if live.get("route_alive") is not True or live.get("simulated") is not False:
        raise ValueError("physical_live_route_required")
    recovery_binding = recovery["binding"]
    qualification_binding = qualification["binding"]
    if recovery_binding["model_id"] != args.target_model_id:
        raise ValueError("target_route_binding_mismatch")
    binding = {
        "deployment_id": recovery_binding["deployment_id"],
        "deployment_epoch": recovery_binding["deployment_epoch"],
        "graph_digest": recovery_binding["graph_digest"],
        "membership_generation": recovery_binding["membership_generation"],
        "model_id": recovery_binding["model_id"],
        "model_revision": recovery_binding["model_revision"],
        "qualification_id": qualification_binding["qualification_id"],
        "qualification_digest": qualification_binding["qualification_digest"],
    }
    target = _model_identity(
        args.target_model_root, args.target_model_id, args.target_revision
    )
    draft = _model_identity(
        args.draft_model_root, args.draft_model_id, args.draft_revision
    )
    timings = [
        float(item["tpot_ms"])
        for item in live.get("recent_inferences", [])
        if isinstance(item, dict)
        and isinstance(item.get("tpot_ms"), (int, float))
        and not isinstance(item.get("tpot_ms"), bool)
    ]
    target_tpot = statistics.median(timings) if timings else None
    stages = live.get("stages", [])
    batched_verification = bool(stages) and all(
        isinstance(stage, dict)
        and stage.get("speculative_target_batch_verification") is True
        for stage in stages
    )
    compatibility = {
        "tokenizer": target["tokenizer_digest"] == draft["tokenizer_digest"],
        "vocabulary": target["vocabulary_size"] == draft["vocabulary_size"],
        "special_tokens": target["special_tokens_digest"]
        == draft["special_tokens_digest"],
        "position_semantics": target["position_semantics_digest"]
        == draft["position_semantics_digest"],
        "separate_kv_ownership": True,
        "batched_target_verification": batched_verification,
    }
    plan = build_speculative_plan(
        binding=binding,
        target=target,
        draft=draft,
        workload_id="interactive-short-live-route",
        proposal_width=4,
        acceptance_distribution=(0.20, 0.20, 0.20, 0.20, 0.20),
        compatibility=compatibility,
        measurements={
            "sample_count": len(timings),
            "target_only_tpot_ms": target_tpot,
            "draft_tpot_ms": None,
            "verification_batch_ms": None,
            "proposal_transfer_ms": None,
            "observed_acceptance_fraction": None,
            "predicted_gain_fraction": None,
            "observed_gain_fraction": None,
        },
    )
    runtime = build_speculative_runtime(binding=binding, mode="disabled", requests=())
    _write(args.deployment_dir / "m20-speculative-plan.json", plan)
    _write(args.deployment_dir / "m20-speculative-runtime.json", runtime)
    report = {
        "protocol": "mycelium.m20_physical_gate.v1",
        "route_alive": True,
        "simulated": False,
        "liveness_digest": liveness["evidence_digest"],
        "target_local_complete": True,
        "draft_local_complete": True,
        "network_download_performed": False,
        "target_only_sample_count": len(timings),
        "target_only_tpot_median_ms": target_tpot,
        "batched_target_verification": batched_verification,
        "decision": plan["decision"],
        "plan_digest": plan["plan_digest"],
        "runtime_digest": runtime["runtime_digest"],
    }
    _write(args.output, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
