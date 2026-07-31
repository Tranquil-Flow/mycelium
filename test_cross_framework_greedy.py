from __future__ import annotations

import math

import pytest

from mycelium_router.decoding import (
    LOGIT_QUANTUM,
    GreedySelectionError,
    quantized_greedy_token_id,
)


def test_cross_framework_near_tie_uses_same_quantized_token() -> None:
    numpy_logits = [
        0.030795196071267128,
        0.030795246362686157,
        0.030795294791460037,
        0.030795343220233917,
        0.030795373022556305,
        0.03079545497894287,
        0.03079545497894287,
    ]
    mlx_logits = [
        0.030795199796557426,
        0.03079523891210556,
        0.030795304104685783,
        0.03079533390700817,
        0.03079538606107235,
        0.03079545684158802,
        0.030795494094491005,
    ]

    assert quantized_greedy_token_id(numpy_logits) == 0
    assert quantized_greedy_token_id(mlx_logits) == 0


def test_quantized_greedy_preserves_clear_winner() -> None:
    assert quantized_greedy_token_id([0.1, 0.1 + 2 * LOGIT_QUANTUM, 0.1]) == 1


def test_quantized_greedy_uses_lowest_token_for_quantized_tie() -> None:
    assert quantized_greedy_token_id([1.0, 1.0, 1.0]) == 0


@pytest.mark.parametrize("values", [[], [math.nan], [math.inf], [True, 0.0]])
def test_quantized_greedy_rejects_invalid_logits(values: list[float]) -> None:
    with pytest.raises(GreedySelectionError):
        quantized_greedy_token_id(values)


@pytest.mark.parametrize("quantum", [0.0, -1.0, math.nan, True])
def test_quantized_greedy_rejects_invalid_quantum(quantum: float) -> None:
    with pytest.raises(GreedySelectionError):
        quantized_greedy_token_id([0.0], quantum=quantum)
