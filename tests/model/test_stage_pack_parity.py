from __future__ import annotations

from pathlib import Path

import mlx.core as mx
import pytest

from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    prepare_physical_deployment,
)
from mycelium_tokenizer.gpt2_bpe import GPT2Tokenizer
from runtime_loader import execute_loaded_stage, load_assignment_stage
from stage_pack import load_fp16_tolerances, verify_stage_pack_collection

SNAPSHOT = Path(
    "~/.cache/huggingface/hub/models--microsoft--DialoGPT-small/snapshots/"
    "49c537161a457d5256512f9d2d38a87d81ae0f0e"
).expanduser()
MODEL_ID = "microsoft/DialoGPT-small"
RESOLVED_COMMIT = "49c537161a457d5256512f9d2d38a87d81ae0f0e"


def _execute_pack_stages(loaded_stages: list[object], token_ids: list[int]) -> mx.array:
    output = execute_loaded_stage(
        loaded_stages[0],
        token_ids=mx.array((token_ids,), dtype=mx.uint32),
    )
    for stage in loaded_stages[1:]:
        output = execute_loaded_stage(stage, hidden_states=output)
    return output


@pytest.mark.parametrize("stage_count", (2, 3, 5))
def test_pack_based_stage_logits_and_greedy_tokens_match_monolithic_reference(
    tmp_path: Path,
    stage_count: int,
) -> None:
    assert SNAPSHOT.is_dir(), "pinned DialoGPT-small snapshot is required"
    deployment = prepare_physical_deployment(
        tmp_path / f"pack-parity-{stage_count}",
        node_ids=tuple(f"node-{index:03d}" for index in range(stage_count)),
        model_source=LocalModelSource(
            root=SNAPSHOT,
            model_id=MODEL_ID,
            requested_revision="local-main",
            resolved_commit=RESOLVED_COMMIT,
        ),
        runtime_dtype="float16",
    )
    model_artifact_digest = dict(deployment.model_artifact_digests)[
        "model.safetensors"
    ]
    tolerance = load_fp16_tolerances(
        Path("tolerances/dialogpt-small-fp16.json"),
        expected_model_id=MODEL_ID,
        expected_resolved_commit=RESOLVED_COMMIT,
        expected_model_artifact_digest=model_artifact_digest,
    )
    ownership = verify_stage_pack_collection(
        deployment.stage_packs,
        assignments=deployment.assignments,
        manifest=deployment.manifest,
    )

    assert ownership["pack_count"] == stage_count
    assert ownership["exact_logical_coverage"] is True
    assert ownership["tied_aliases"] == []
    logical_owned_sets = [
        set(record["tensor_keys"])
        for record in ownership["logical_owned_tensor_keys"]
    ]
    assert all(
        left.isdisjoint(right)
        for index, left in enumerate(logical_owned_sets)
        for right in logical_owned_sets[index + 1 :]
    )
    assert {
        record["upstream_path"]
        for record in ownership["shared_backing_artifacts"]
    } == {"model.safetensors"}
    assert all("stage_pack" in report for report in deployment.artifact_reports)
    assert "stage_pack" not in deployment.reference_report

    loaded_stages = [
        load_assignment_stage(assignment, report, load_generation=17)
        for assignment, report in zip(
            deployment.assignments,
            deployment.artifact_reports,
            strict=True,
        )
    ]
    loaded_reference = load_assignment_stage(
        deployment.reference_assignment,
        deployment.reference_report,
        load_generation=17,
    )
    for stage, pack in zip(
        loaded_stages,
        deployment.stage_packs,
        strict=True,
    ):
        assert stage.proof["stage_pack_digest"] == pack["stage_pack_digest"]

    context = GPT2Tokenizer.from_files(
        SNAPSHOT / "vocab.json",
        SNAPSHOT / "merges.txt",
    ).encode("Hello, how are you?")
    generated: list[int] = []
    max_abs_diff = 0.0
    logits_policy = tolerance["checks"]["logits"]
    for _ in range(4):
        pack_logits = _execute_pack_stages(loaded_stages, context)
        reference_logits = execute_loaded_stage(
            loaded_reference,
            token_ids=mx.array((context,), dtype=mx.uint32),
        )
        mx.eval(pack_logits, reference_logits)
        delta = mx.abs(pack_logits - reference_logits)
        mx.eval(delta)
        max_abs_diff = max(max_abs_diff, float(mx.max(delta).item()))
        assert bool(
            mx.allclose(
                pack_logits,
                reference_logits,
                rtol=logits_policy["relative_tolerance"],
                atol=logits_policy["absolute_tolerance"],
            ).item()
        )

        pack_token = int(mx.argmax(pack_logits[0, -1, :]).item())
        reference_token = int(mx.argmax(reference_logits[0, -1, :]).item())
        assert pack_token == reference_token
        generated.append(pack_token)
        context.append(pack_token)

    print(
        f"stage_count={stage_count} max_abs_logit_diff={max_abs_diff:.9g} "
        f"greedy_token_ids={generated}"
    )
    assert max_abs_diff == 0.0
    assert generated == [4599, 3329, 2506, 5145]
