# SPDX-License-Identifier: AGPL-3.0-only
"""Measure the Rust Qwen2 stage against the NumPy serving path on real weights.

    cargo build --release --example qwen2_stage_exec   (in native/mobile_core)
    MYCELIUM_QWEN05_SNAPSHOT=/path/to/snapshot \\
        python3 -B native/mobile_core/tests/fixtures/qwen2_host_agreement.py OUT.json [--new-tokens 24]

Reports, for Qwen2.5-0.5B-Instruct:
1. per-layer parity: every layer run by the Rust host executor on the NumPy
   input of that layer, max abs diff vs NumPy (no compounding);
2. end-to-end: all 24 layers in Rust from the NumPy embedding, final-hidden
   and logit differences;
3. greedy token agreement with complete-context replay, the router's
   ``quantized_greedy_token_id``, for three routes per prompt:
   ``numpy`` (reference), ``rust_all`` (Rust layers 0-24) and ``rust_12_14``
   (NumPy 0-12, Rust 12-14 as a two-layer phone stage, NumPy 14-24).

Embedding, final norm and lm_head (tied, int8) always run in NumPy, exactly
as a NumPy entry/exit node would. A measurement tool, not a test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from generate_qwen2_fixtures import (  # noqa: E402
    MATRICES,
    VECTORS,
    ROOT,
    block_config,
    load_numpy,
    numpy_layer,
)

import numpy_runtime  # noqa: E402
from mycelium_router.decoding import quantized_greedy_token_id  # noqa: E402

CRATE = ROOT / "native" / "mobile_core"
PROMPTS = (
    "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\nWhat is the capital of France? Answer in one sentence.<|im_end|>\n<|im_start|>assistant\n",
    "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\nWrite a haiku about mushrooms growing after rain.<|im_end|>\n<|im_start|>assistant\n",
    "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\nExplain in two sentences why the sky is blue.<|im_end|>\n<|im_start|>assistant\n",
    "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\nList the first ten prime numbers, separated by commas.<|im_end|>\n<|im_start|>assistant\n",
)
EOS = {151643, 151645}


class RustStage:
    def __init__(self, snapshot: Path, start: int, end: int, workdir: Path):
        exe = CRATE / "target" / "release" / "examples" / "qwen2_stage_exec"
        self.proc = subprocess.Popen(
            [str(exe), str(snapshot), str(start), str(end)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.ready = json.loads(self.proc.stdout.readline())
        self.dir = workdir
        self.forward_ms: list[tuple[int, int, float]] = []

    def run(self, hidden: np.ndarray, start: int, end: int) -> np.ndarray:
        seq = int(hidden.shape[1])
        src, dst = self.dir / "in.f32", self.dir / "out.f32"
        np.ascontiguousarray(hidden, dtype="<f4").tofile(src)
        request = {"seq": seq, "input": str(src), "output": str(dst), "from": start, "to": end}
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        if not reply.get("ok"):
            raise RuntimeError(reply)
        self.forward_ms.append((seq, end - start, reply["forward_ms"]))
        return np.fromfile(dst, dtype="<f4").reshape(hidden.shape)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=30)


def main() -> None:
    out_path = Path(sys.argv[1])
    new_tokens = int(sys.argv[sys.argv.index("--new-tokens") + 1]) if "--new-tokens" in sys.argv else 24
    snapshot = Path(os.environ["MYCELIUM_QWEN05_SNAPSHOT"])
    config = json.loads((snapshot / "config.json").read_text())
    n_layer = int(config["num_hidden_layers"])
    keys = {"model.embed_tokens.weight", "model.norm.weight"}
    for layer in range(n_layer):
        keys |= {f"model.layers.{layer}.{s}" for s in MATRICES + VECTORS}
    tensors = load_numpy(snapshot / "model.safetensors", keys)
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(snapshot / "tokenizer.json"))
    eps = float(config["rms_norm_eps"])

    def embed(ids: list[int]) -> np.ndarray:
        return numpy_runtime._qwen2_embedding(
            tensors["model.embed_tokens.weight"], np.asarray([ids], dtype=np.int64)
        ).astype(np.float32)

    def head(hidden: np.ndarray) -> np.ndarray:
        last = numpy_runtime._rms_norm(hidden[:, -1:, :], tensors["model.norm.weight"], eps)
        return numpy_runtime._qwen2_linear_checkpointed(
            last, tensors["model.embed_tokens.weight"], checkpoint=lambda: None
        )[0, -1, :]

    def numpy_layers(hidden: np.ndarray, start: int, end: int) -> np.ndarray:
        for layer in range(start, end):
            hidden = numpy_layer(hidden, tensors, layer, config)
        return hidden

    report: dict[str, object] = {
        "model": "Qwen/Qwen2.5-0.5B-Instruct",
        "resolved_commit": snapshot.name,
        "numpy_version": np.__version__,
        "block_config": block_config(config),
    }
    with tempfile.TemporaryDirectory() as tmp:
        rust = RustStage(snapshot, 0, n_layer, Path(tmp))
        report["rust_load"] = rust.ready

        # 1 + 2: per-layer and end-to-end parity on the first prompt.
        ids = tokenizer.encode(PROMPTS[0], add_special_tokens=False).ids
        hidden = embed(ids)
        per_layer = []
        rust_chain = hidden
        for layer in range(n_layer):
            reference = numpy_layer(hidden, tensors, layer, config)
            ours = rust.run(hidden, layer, layer + 1)
            per_layer.append({
                "layer": layer,
                "max_abs_diff": float(np.max(np.abs(ours - reference))),
                "max_abs_ref": float(np.max(np.abs(reference))),
                "max_abs_layer_delta": float(np.max(np.abs(reference - hidden))),
            })
            hidden = reference
        rust_chain = rust.run(embed(ids), 0, n_layer)
        logits_np, logits_rs = head(hidden), head(rust_chain)
        report["prompt0_seq"] = len(ids)
        report["per_layer"] = per_layer
        report["end_to_end"] = {
            "final_hidden_max_abs_diff": float(np.max(np.abs(rust_chain - hidden))),
            "final_hidden_max_abs_ref": float(np.max(np.abs(hidden))),
            "logits_max_abs_diff": float(np.max(np.abs(logits_rs - logits_np))),
            "logits_max_abs_ref": float(np.max(np.abs(logits_np))),
            "top1_equal": int(np.argmax(logits_np)) == int(np.argmax(logits_rs)),
        }

        # 3: greedy decode, complete-context replay, three routes.
        routes = {
            "numpy": lambda h: numpy_layers(h, 0, n_layer),
            "rust_all": lambda h: rust.run(h, 0, n_layer),
            "rust_12_14": lambda h: numpy_layers(rust.run(numpy_layers(h, 0, 12), 12, 14), 14, n_layer),
        }
        greedy = []
        for prompt in PROMPTS:
            prompt_ids = tokenizer.encode(prompt, add_special_tokens=False).ids
            result: dict[str, object] = {"prompt_tokens": len(prompt_ids)}
            for name, route in routes.items():
                ids = list(prompt_ids)
                generated, margins = [], []
                started = time.perf_counter()
                for _ in range(new_tokens):
                    logits = head(route(embed(ids)))
                    token = quantized_greedy_token_id(logits.tolist())
                    top2 = np.sort(logits)[-2:]
                    margins.append(float(top2[1] - top2[0]))
                    generated.append(token)
                    ids.append(token)
                    if token in EOS:
                        break
                result[name] = {
                    "tokens": generated,
                    "text": tokenizer.decode(generated),
                    "min_top2_margin": min(margins),
                    "seconds": round(time.perf_counter() - started, 2),
                }
            reference = result["numpy"]["tokens"]
            for name in ("rust_all", "rust_12_14"):
                tokens = result[name]["tokens"]
                same = sum(1 for a, b in zip(tokens, reference) if a == b)
                first = next((i for i, (a, b) in enumerate(zip(tokens, reference)) if a != b), None)
                result[name]["agreement"] = f"{same}/{max(len(tokens), len(reference))}"
                result[name]["identical"] = tokens == reference
                result[name]["first_divergence"] = first
            greedy.append(result)
        report["greedy"] = greedy
        timing: dict[str, list[float]] = {}
        for seq, layers, ms in rust.forward_ms:
            timing.setdefault(f"seq{seq}", []).append(ms / layers)
        report["rust_ms_per_layer_by_seq"] = {k: round(float(np.median(v)), 2) for k, v in sorted(timing.items())}
        rust.close()
    out_path.write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({"per_layer_max_abs_diff": max(p["max_abs_diff"] for p in per_layer),
                      "end_to_end": report["end_to_end"],
                      "greedy": [{k: (v["agreement"] if isinstance(v, dict) and "agreement" in v else None)
                                  for k, v in g.items() if k.startswith("rust")} for g in greedy]}, indent=1))


if __name__ == "__main__":
    main()
