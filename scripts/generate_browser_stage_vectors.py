#!/usr/bin/env python3.14
"""Generate deterministic Python PixelStage vectors for the browser worker."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_interactive.swarm import matrix_digest  # noqa: E402
from mycelium_mobile.pixel_stage import PixelStage, build_stage_pack  # noqa: E402

OUTPUT = ROOT / "ui" / "web" / "src" / "test" / "browserStageVectors.json"


def values(rows: int, columns: int, *, offset: int, scale: float) -> list[list[float]]:
    return [
        [((row * columns + column + offset) % 17 - 8) * scale for column in range(columns)]
        for row in range(rows)
    ]


def vector(width: int, *, offset: int, scale: float) -> list[float]:
    return [((index + offset) % 13 - 6) * scale for index in range(width)]


def build_document() -> dict[str, Any]:
    hidden = 4
    inner = 8
    prefix = "transformer.h.1."
    tensors = {
        prefix + "ln_1.weight": [1.0, 0.9, 1.1, 1.05],
        prefix + "ln_1.bias": vector(hidden, offset=1, scale=0.01),
        prefix + "attn.c_attn.weight": values(hidden, 3 * hidden, offset=2, scale=0.017),
        prefix + "attn.c_attn.bias": vector(3 * hidden, offset=3, scale=0.013),
        prefix + "attn.c_proj.weight": values(hidden, hidden, offset=4, scale=0.019),
        prefix + "attn.c_proj.bias": vector(hidden, offset=5, scale=0.011),
        prefix + "ln_2.weight": [1.0, 1.02, 0.98, 1.01],
        prefix + "ln_2.bias": vector(hidden, offset=6, scale=0.007),
        prefix + "mlp.c_fc.weight": values(hidden, inner, offset=7, scale=0.015),
        prefix + "mlp.c_fc.bias": vector(inner, offset=8, scale=0.009),
        prefix + "mlp.c_proj.weight": values(inner, hidden, offset=9, scale=0.014),
        prefix + "mlp.c_proj.bias": vector(hidden, offset=10, scale=0.008),
    }
    return build_stage_pack(
        run_id="browser-vector-run",
        deployment_id="browser-vector-deployment",
        assignment_id="browser-vector-assignment",
        stage_id="browser-vector-stage",
        model_id="browser-vector-model",
        resolved_commit="0123456789abcdef0123456789abcdef01234567",
        manifest_digest="sha256:" + "1" * 64,
        parent_assignment_digest="sha256:" + "2" * 64,
        parent_load_proof_digest="sha256:" + "3" * 64,
        start_layer=1,
        end_layer_exclusive=2,
        n_head=2,
        hidden_size=hidden,
        epsilon=1e-5,
        activation_function="gelu_new",
        scale_attn_weights=True,
        scale_attn_by_inverse_layer_idx=False,
        reorder_and_upcast_attn=False,
        add_cross_attention=False,
        tensors=tensors,
    )


def fixture() -> dict[str, Any]:
    pack = build_document()
    stage = PixelStage.from_document(pack)
    inputs = [
        [[0.1, -0.2, 0.3, -0.4]],
        [
            [0.1, 0.2, 0.3, 0.4],
            [-0.3, 0.4, -0.5, 0.6],
            [0.7, -0.8, 0.9, -1.0],
        ],
        [
            [0.01, 0.02, 0.03, 0.04],
            [1.0, 0.5, -0.5, -1.0],
            [0.25, -0.25, 0.75, -0.75],
            [-0.9, 0.8, -0.7, 0.6],
        ],
    ]
    vectors = []
    for index, hidden in enumerate(inputs):
        output = stage.execute(
            request_id=f"vector-{index}",
            assignment_id=pack["assignment_id"],
            stage_id=pack["stage_id"],
            hidden=hidden,
        )
        vectors.append(
            {
                "input": hidden,
                "input_digest": matrix_digest(hidden),
                "output": output,
                "output_digest": matrix_digest(output),
            }
        )
    return {
        "protocol": "mycelium.browser_stage_vectors.v1",
        "route_ready": False,
        "pack": pack,
        "vectors": vectors,
    }


def encoded() -> bytes:
    return (json.dumps(fixture(), indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    expected = encoded()
    if arguments.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != expected:
            print("browser stage vectors drift", file=sys.stderr)
            return 1
        print("browser stage vectors OK")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(expected)
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
