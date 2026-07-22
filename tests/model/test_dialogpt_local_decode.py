from pathlib import Path

import mlx.core as mx
import pytest

from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    PhysicalDeployment,
    prepare_physical_deployment,
)
from mycelium_tokenizer.gpt2_bpe import GPT2Tokenizer
from runtime_loader import execute_loaded_stage, load_assignment_stage

SNAPSHOT = Path(
    "~/.cache/huggingface/hub/models--microsoft--DialoGPT-small/snapshots/"
    "49c537161a457d5256512f9d2d38a87d81ae0f0e"
).expanduser()

pytestmark = pytest.mark.skipif(
    not SNAPSHOT.is_dir(), reason="DialoGPT snapshot not present"
)


def _greedy_decode(
    deployment: PhysicalDeployment,
    prompt_ids: list[int],
    *,
    max_new_tokens: int,
) -> list[int]:
    stage = load_assignment_stage(
        deployment.reference_assignment,
        deployment.reference_report,
        load_generation=17,
    )
    context = list(prompt_ids)
    generated: list[int] = []
    for _ in range(max_new_tokens):
        tokens = mx.array((context,), dtype=mx.uint32)
        logits = execute_loaded_stage(stage, token_ids=tokens)
        mx.eval(logits)
        token_id = int(mx.argmax(logits[0, -1, :]).item())
        generated.append(token_id)
        context.append(token_id)
    return generated


def test_monolithic_reference_generates_coherent_tokens(tmp_path: Path) -> None:
    deployment = prepare_physical_deployment(
        tmp_path / "deployment",
        node_ids=("node-a", "node-b"),
        model_source=LocalModelSource(
            root=SNAPSHOT,
            model_id="microsoft/DialoGPT-small",
            requested_revision="local-main",
            resolved_commit="49c537161a457d5256512f9d2d38a87d81ae0f0e",
        ),
        runtime_dtype="float16",
    )
    tokenizer = GPT2Tokenizer.from_files(
        SNAPSHOT / "vocab.json", SNAPSHOT / "merges.txt"
    )
    prompt_ids = tokenizer.encode("Hello, how are you?")

    generated = _greedy_decode(deployment, prompt_ids, max_new_tokens=12)
    text = tokenizer.decode(generated)

    assert len(generated) == 12
    assert text.strip(), "decoded text must not be empty"
    print(f"PROMPT: Hello, how are you?\nCOMPLETION: {text}")
