from pathlib import Path

import pytest

from distributed_inference_qualification import qualify_distributed_decode
from mycelium_qualification.physical_deployment import LocalModelSource
from mycelium_tokenizer.gpt2_bpe import GPT2Tokenizer

SNAPSHOT = Path(
    "~/.cache/huggingface/hub/models--microsoft--DialoGPT-small/snapshots/"
    "49c537161a457d5256512f9d2d38a87d81ae0f0e"
).expanduser()


@pytest.mark.parametrize("node_count", (2, 3, 5))
def test_sharded_local_processes_match_monolithic_greedy_decode(
    tmp_path: Path,
    node_count: int,
) -> None:
    if not SNAPSHOT.exists():
        pytest.skip("pinned DialoGPT-small snapshot is not installed")

    prompt_ids = GPT2Tokenizer.from_files(
        SNAPSHOT / "vocab.json", SNAPSHOT / "merges.txt"
    ).encode("Hello, how are you?")
    result = qualify_distributed_decode(
        tmp_path,
        node_count=node_count,
        model_source=LocalModelSource(
            root=SNAPSHOT,
            model_id="microsoft/DialoGPT-small",
            resolved_commit="49c537161a457d5256512f9d2d38a87d81ae0f0e",
        ),
        prompt_token_ids=prompt_ids,
        max_new_tokens=4,
        seed=17,
    )

    assert result.node_count == node_count
    assert result.seed == 17
    assert len(result.layer_ranges) == node_count
    assert len(result.worker_pids) == node_count
    assert len(set(result.worker_pids)) == node_count
    assert result.distributed_token_ids == result.reference_token_ids
