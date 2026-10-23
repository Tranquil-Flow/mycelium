# SPDX-License-Identifier: AGPL-3.0-only
"""Single-producer ACK-clocked receipt ranges; no extra native reader/thread.

Only authenticated journal eviction opens the next slot. The compute thread
buffers unsigned tokens, not signed receipts; its terminal drain is bounded by
the original grant and cancellation fence. ACK handlers never publish or call
back into authority. A failed drain leaves reconciliation to the owner.
"""
from .token_path import TokenBatch
from .token_path_authority import RECEIPT_PROTOCOL, _ReceiptState


class CoalescedReceiptDelivery:
    def __init__(self, journal, publish, fence):
        self.journal, self.publish, self.fence = journal, publish, fence
        self.source = _ReceiptState(journal.grant, journal._clock, journal._current)
        self.tokens = []
        self.sequence = 0
        self.index = 0
        self.terminal = None
        self.finished = False

    def submit(self, batch):
        self.fence()
        body = dict(protocol=RECEIPT_PROTOCOL, grant_digest=self.journal.grant.digest,
            previous_receipt_digest=self.source._previous_digest,
            sequence=batch.sequence, start_index=batch.start_index,
            end_index=batch.start_index+len(batch.token_ids), token_ids=list(batch.token_ids),
            status=batch.status, error_code=None, stage_receipt_refs=[])
        # Validate the producer's original ranges as well as the coalesced wire
        # ranges. Coalescing must never hide a gap, overlap, or budget violation.
        self.source._validate(body)
        self.source._advance(body)
        self.tokens.extend(batch.token_ids)
        if batch.status != 'DECODING':
            self.terminal = batch.status
        self.flush()

    def flush(self):
        self.fence()
        # Membership/peer revocation is independent of request-control equality.
        # Recheck even when an outstanding ACK prevents another append.
        self.source._fence()
        if self.finished or not self.journal.delivery_ready():
            return
        if not self.tokens and self.terminal is None:
            return
        # Failure/cancellation terminals never carry tokens. Preserve any
        # preceding verified-order prefix as a separate range first.
        status = self.terminal or 'DECODING'
        if self.tokens and status in {'CANCELLED', 'FAILED'}:
            status = 'DECODING'
        batch = TokenBatch(self.sequence, self.index, tuple(self.tokens), status)
        receipt = self.journal.append(batch)
        self.fence()
        self.publish(receipt)
        self.fence()
        self.index += len(self.tokens)
        self.tokens.clear()
        self.sequence += 1
        self.finished = status != 'DECODING'

    def finish(self):
        # No receipt-count limit and no new timeout. A stopped/revoked/expired
        # request still fails closed; a healthy slow delivery grows one range.
        while not self.finished:
            self.fence()
            self.flush()
            if not self.finished:
                self.journal.wait_for_delivery_slot(.05)
