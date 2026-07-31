"""Framework-neutral greedy token selection.

Floating-point kernels can differ by a few ULPs across otherwise equivalent
backends. Raw argmax makes near-tied logits choose different tokens, after which
autoregressive decode diverges completely. Quantizing before argmax freezes one
portable policy while preserving ordinary lowest-token tie breaking.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

LOGIT_QUANTUM = 1e-5


class GreedySelectionError(ValueError):
    """Raised when logits cannot be decoded under the portable contract."""


def quantized_greedy_token_id(
    logits: Iterable[float],
    *,
    quantum: float = LOGIT_QUANTUM,
) -> int:
    """Return lowest token ID in the highest quantized logit bucket.

    The function deliberately uses only Python scalars. Runtime adapters must
    materialize their final logit row before calling it, keeping this contract
    independent of NumPy, MLX, device type, and architecture.
    """

    if (
        not isinstance(quantum, (int, float))
        or isinstance(quantum, bool)
        or not math.isfinite(float(quantum))
        or float(quantum) <= 0.0
    ):
        raise GreedySelectionError("invalid_logit_quantum")
    try:
        values = tuple(logits)
    except TypeError as exc:
        raise GreedySelectionError("invalid_logits") from exc
    if not values:
        raise GreedySelectionError("empty_logits")
    normalized: list[float] = []
    for value in values:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            raise GreedySelectionError("invalid_logit")
        normalized.append(float(value))
    buckets = [round(value / float(quantum)) for value in normalized]
    return max(range(len(buckets)), key=buckets.__getitem__)
