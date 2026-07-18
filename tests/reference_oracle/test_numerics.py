from __future__ import annotations

from pathlib import Path

import mlx.core as mx

from conftest import (
    EIGHT_STEP_PROMPT,
    FIRST_TOKEN_PROMPT,
    MODEL_ID,
    RESOLVED_COMMIT,
)
from mycelium_reference_oracle.gpt2 import (
    ABSOLUTE_TOLERANCE,
    RELATIVE_TOLERANCE,
    load_gpt2_fixture,
)

EXPECTED_LAYER_HIDDEN_STATES = (
    (
        (0.08485989272594452, 0.1000918447971344, 0.11532380431890488, 0.13055574893951416),
        (0.1448599100112915, 0.1600918471813202, 0.17532381415367126, 0.19055573642253876),
        (0.2048598974943161, 0.22009184956550598, 0.23532381653785706, 0.25055575370788574),
    ),
    (
        (0.13642136752605438, 0.15190157294273376, 0.16738177835941315, 0.18286196887493134),
        (0.1964213252067566, 0.21190153062343597, 0.22738175094127655, 0.24286192655563354),
        (0.25642135739326477, 0.27190154790878296, 0.28738173842430115, 0.30286192893981934),
    ),
)
EXPECTED_FIRST_TOKEN_LOGITS = (
    0.030795037746429443,
    0.030794939026236534,
    0.030794847756624222,
    0.030794737860560417,
    0.03079465590417385,
    0.030794579535722733,
    0.030794454738497734,
)
EXPECTED_EIGHT_TOKENS = (6, 6, 6, 2, 0, 0, 0, 0)


def oracle(root: Path):
    return load_gpt2_fixture(
        root,
        model_id=MODEL_ID,
        resolved_commit=RESOLVED_COMMIT,
    )


def assert_close(actual: mx.array, expected: mx.array) -> None:
    assert bool(
        mx.allclose(
            actual,
            expected,
            rtol=RELATIVE_TOLERANCE,
            atol=ABSOLUTE_TOLERANCE,
        ).item()
    )


def test_independent_first_token_logits_and_greedy_token_match_frozen_fixture(
    fixture_dir: Path,
) -> None:
    result = oracle(fixture_dir).forward(FIRST_TOKEN_PROMPT)

    assert tuple(result.logits.shape) == (1, 3, 7)
    assert_close(
        result.logits[0, -1, :],
        mx.array(EXPECTED_FIRST_TOKEN_LOGITS, dtype=mx.float32),
    )
    assert result.greedy_token_id == 0


def test_every_layer_hidden_state_shape_and_values_match_frozen_fixture(
    fixture_dir: Path,
) -> None:
    result = oracle(fixture_dir).forward(FIRST_TOKEN_PROMPT)

    assert len(result.layer_hidden_states) == 2
    for actual, expected in zip(
        result.layer_hidden_states,
        EXPECTED_LAYER_HIDDEN_STATES,
    ):
        assert tuple(actual.shape) == (1, 3, 4)
        assert_close(actual, mx.array((expected,), dtype=mx.float32))


def test_generates_exactly_eight_frozen_greedy_tokens(fixture_dir: Path) -> None:
    result = oracle(fixture_dir).greedy_decode(EIGHT_STEP_PROMPT, steps=8)

    assert result.generated_token_ids == EXPECTED_EIGHT_TOKENS
    assert len(result.steps) == 8
    assert tuple(step.token_id for step in result.steps) == EXPECTED_EIGHT_TOKENS
    assert all(step.logits_digest.startswith("sha256:") for step in result.steps)
    assert all(len(step.activation_digests) == 2 for step in result.steps)
