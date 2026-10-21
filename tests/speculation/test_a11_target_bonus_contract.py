# SPDX-License-Identifier: AGPL-3.0-only
"""Owner amendment 2026-10-07: the runtime contract counts the target bonus.

After an all-match span the target's own next decision may be committed.  It is
target-drawn, so ``committed = accepted + corrected + target_bonus``; a bonus needs
an accepted span before it, and a target-only request carries none.  Documents
without the field keep their meaning (zero bonus).
"""

from __future__ import annotations

import pytest

import mycelium_speculative_decode as sd
from tests.a11_scaffold.test_a11_contract_scaffold import _runtime, _runtime_request


def _all_match(**overrides):
    values = {
        "proposed_count": 4, "verified_count": 4, "accepted_count": 4, "rejected_count": 0,
        "target_corrected_count": 0, "rollback_count": 0, "committed_count": 5,
        "target_committed_output_tokens": 5, "target_bonus_count": 1,
    }
    values.update(overrides)
    return _runtime_request(**values)


def test_a_counted_bonus_is_target_authoritative() -> None:
    evaluated = sd.validate_speculative_decode_runtime(_runtime(requests=[_all_match()]))
    assert evaluated["requests"][0]["target_bonus_count"] == 1


def test_documents_without_the_field_keep_their_meaning() -> None:
    sd.validate_speculative_decode_runtime(_runtime(requests=[_runtime_request()]))
    with pytest.raises(sd.SpeculativeDecodeContractError, match="committed_positions_not_target_authoritative"):
        sd.validate_speculative_decode_runtime(
            _runtime(requests=[_all_match(target_bonus_count=0)])
        )


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"committed_count": 6, "target_committed_output_tokens": 6}, "committed_positions_not_target_authoritative"),
        ({"target_bonus_count": 5, "committed_count": 9, "target_committed_output_tokens": 9}, "impossible_runtime_counts"),
    ],
)
def test_bonus_counts_must_add_up(overrides, code) -> None:
    with pytest.raises(sd.SpeculativeDecodeContractError, match=code):
        sd.validate_speculative_decode_runtime(_runtime(requests=[_all_match(**overrides)]))


@pytest.mark.parametrize("value", [-1, True, 1.0, None])
def test_bonus_count_is_a_non_negative_integer(value) -> None:
    with pytest.raises(ValueError):
        sd.validate_speculative_decode_runtime(_runtime(requests=[_all_match(target_bonus_count=value)]))


def test_target_only_requests_carry_no_bonus() -> None:
    request = _runtime_request(
        mode="target_only", proposal_width=0, proposed_count=0, verified_count=0, accepted_count=0,
        rejected_count=0, target_corrected_count=0, rollback_count=0, committed_count=0,
        target_committed_output_tokens=0, deadline_outcome="none", draft_queue_ms=None,
        draft_compute_ms=None, transfer_ms=None, target_bonus_count=1,
    )
    with pytest.raises(ValueError):
        sd.validate_speculative_decode_runtime(_runtime(mode="target_only", requests=[request]))
