# SPDX-License-Identifier: AGPL-3.0-only
"""Stop changes terminal intent, not the existing cleanup authority window."""
from dataclasses import replace

import pytest

from mycelium_live.command_controller import CommandController, TerminalStatus
from tests.live.test_a4_commands import _envelope, _cleanup, _terminal, DIGEST_D


@pytest.mark.parametrize('receipt_first', [False, True])
def test_stop_preserves_original_completion_cleanup_proof(receipt_first):
    controller = CommandController()
    envelope = _envelope()
    assert controller.register(envelope).accepted
    accepted = controller.cancel(envelope.identity, new_cancellation_generation=1,
        observed_at_ms=2000, idempotency_digest=DIGEST_D, completion_cleanup=True)
    assert accepted.accepted
    identity = accepted.snapshot.identity
    def record(at=2100):
        return controller.record_cleanup(identity, owner_id=envelope.cleanup_owner_id,
            result=_cleanup(), observed_at_ms=at)
    if receipt_first:
        assert record().accepted
    promoted = controller.promote_completion_cleanup_to_cancellation(identity, observed_at_ms=2300)
    assert promoted.accepted
    # The delayed/repeated exact receipt remains valid; no new generation,
    # deadline, proof timestamp or cleanup CAS is invented for owner Stop.
    receipt = record()
    assert receipt.accepted, receipt.reason
    assert receipt.duplicate is receipt_first
    assert promoted.snapshot.cleanup_deadline_ms == accepted.snapshot.cleanup_deadline_ms
    assert promoted.snapshot.identity == identity
    assert record(1999).reason == 'cleanup_before_authority'
    assert controller.record_cleanup(replace(identity, path_attempt=1),
        owner_id=envelope.cleanup_owner_id, result=_cleanup(), observed_at_ms=2100).accepted is False
    assert controller.terminal_compare_and_swap(_terminal(identity, status=TerminalStatus.CANCELLED,
        observed_at_ms=2400), expected_terminal_revision=0).accepted
