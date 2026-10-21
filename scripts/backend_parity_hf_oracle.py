#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Offline HF float32 CPU oracle for the backend parity gate (PV29 L1).

Standalone on purpose: it needs only torch + transformers + numpy (it does NOT import the
Mycelium packages, so it runs in any torch venv).  It never participates in a route and never
downloads (HF offline mode is forced).

    python scripts/backend_parity_hf_oracle.py --snapshot <local HF snapshot dir> \
        --corpus tolerances/backend-parity-corpus-qwen2.5-0.5b-instruct.v1.json \
        --out oracle.npz [--boundaries 12]

For every corpus case it runs ONE full-context float32 forward (no KV cache, teacher-forced on
the corpus token ids) and stores the logits at the scored positions (prefill_len-1 .. len-2) and
the hidden state after each requested layer boundary for positions 0..len-2.  Writes
``<out>`` (npz: ``logits__<case>``, ``hidden__<case>__<b>``) and ``<out>.json`` (identity + digests).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

import numpy as np

CORPUS_PROTOCOL = "mycelium.backend_parity_corpus.v1"
IMPLEMENTATION = "hf-transformers-float32-cpu-teacher-forced"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return "sha256:" + sha.hexdigest()


def scored_positions(case: Mapping[str, Any]) -> list[int]:
    return list(range(case["prefill_len"] - 1, len(case["token_ids"]) - 1))


def build_archive(
    cases: Mapping[str, tuple[np.ndarray, Mapping[int, np.ndarray]]],
) -> dict[str, np.ndarray]:
    """{case_id: (logits[n_scored,V], {boundary: hidden[L-1,H]})} -> npz arrays (the load_oracle format)."""

    arrays: dict[str, np.ndarray] = {}
    for case_id, (logits, hidden) in cases.items():
        arrays[f"logits__{case_id}"] = np.asarray(logits, dtype=np.float32)
        for boundary, rows in hidden.items():
            arrays[f"hidden__{case_id}__{boundary}"] = np.asarray(rows, dtype=np.float32)
    return arrays


def write_archive(out: Path, arrays: Mapping[str, np.ndarray], runtime: Mapping[str, Any]) -> dict[str, Any]:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        np.savez(handle, **arrays)
    info = {
        "implementation": IMPLEMENTATION,
        "runtime_digest": _digest_bytes(_canonical(dict(runtime))),
        "archive_digest": _file_digest(out),
        "dtype": "float32",
    }
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--boundaries", default="12")
    args = parser.parse_args(argv)

    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    if corpus.get("protocol") != CORPUS_PROTOCOL:
        print("error: corpus protocol mismatch", file=sys.stderr)
        return 2
    corpus_digest = _digest_bytes(_canonical(corpus))
    boundaries = [int(x) for x in args.boundaries.split(",")]

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    import transformers
    from transformers import AutoModelForCausalLM

    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    model = AutoModelForCausalLM.from_pretrained(args.snapshot, local_files_only=True, trust_remote_code=False, dtype=torch.float32)
    model.eval()
    results: dict[str, tuple[np.ndarray, dict[int, np.ndarray]]] = {}
    with torch.inference_mode():
        for case in corpus["cases"]:
            ids = torch.tensor([case["token_ids"]], dtype=torch.long)
            out = model(input_ids=ids, use_cache=False, output_hidden_states=True, return_dict=True)
            logits = out.logits[0, scored_positions(case), :].float().numpy().astype(np.float32)
            hidden = {b: out.hidden_states[b][0, :-1, :].float().numpy().astype(np.float32) for b in boundaries}
            if not np.isfinite(logits).all():
                print(f"error: non-finite oracle logits in {case['case_id']}", file=sys.stderr)
                return 1
            results[case["case_id"]] = (logits, hidden)
    runtime = {
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "numpy": importlib.metadata.version("numpy"),
        "dtype": "float32",
        "device": "cpu",
    }
    info = write_archive(Path(args.out), build_archive(results), runtime)
    info["corpus_digest"] = corpus_digest
    info["snapshot_name"] = Path(args.snapshot).resolve().name
    Path(str(args.out) + ".json").write_text(json.dumps(info, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"oracle: {len(results)} cases -> {args.out}\n  archive {info['archive_digest']}\n  runtime {info['runtime_digest']}\n  corpus  {corpus_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
