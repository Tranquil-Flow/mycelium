# SPDX-License-Identifier: AGPL-3.0-only
"""The A6 successor challenge must reproduce the exact startup output.

Run day (Qwen2.5 int8): the controller-owned startup output was
``[19202, 13, 151645, 198]``; ``151645`` is ``<|im_end|>``, a product stop
token. Product stop filtering truncated the successor challenge to two tokens,
so every successor failed qualification and A6 recovery was refused with
``recovery_physical_qualification_required``. Real local node processes.
"""
from __future__ import annotations

import time

from tests.live import test_a6_local_physical_recovery as base
from tests.live.test_a6_local_physical_recovery import watchdog  # noqa: F401

# Importing a helper does not inherit its module's pytest marks. This scenario
# requires the same debug sidecar as the base's real local-process scenarios.
pytestmark = base.pytestmark


def test_successor_challenge_ignores_product_stop_tokens(tmp_path, watchdog) -> None:  # noqa: F811
    local, owner, _router, service, target = base._compose(tmp_path, "full_replica")
    try:
        expected = local.reference_tokens
        # Configure the product stop set exactly as serve does from the codec,
        # with a token the startup challenge output contains.
        owner.set_stop_token_ids((expected[len(expected) // 2],))
        far = int(time.time() * 1_000) + 60_000
        for placement_id in ("placement-001", "placement-000"):
            challenge = target.qualifier._run_physical_challenge(
                request_id=f"stop-token-{placement_id}",
                placement_ids=(placement_id,),
                absolute_deadline_unix_ms=far,
            )
            outcome = target.qualifier.physical_challenge_outcome(
                f"stop-token-{placement_id}")
            assert challenge["passed"] is True, outcome
            assert outcome["observed_token_count"] == len(expected), outcome
    finally:
        service.close()
        owner.close()
        local.close()
