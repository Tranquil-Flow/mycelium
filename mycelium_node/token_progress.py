# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded native receipt framing, not managed-mailbox authority.

The native pipe is private transport. Its frame authenticates nothing by itself:
ReceiptInbox requires an independently pinned ReceiptCursor before returning any
tokens. Reads never ACK, update stage liveness, or authorize cleanup. The caller
must seal evidence and deliver authenticated ACKs on the eventual owner channel.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import re
import threading
import time
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_router.token_path_authority import ReceiptCursor, MAX_RECEIPT_BYTES

PROGRESS_PROTOCOL = 'mycelium.physical_node_progress.v1'
MAX_PROGRESS_BYTES = MAX_RECEIPT_BYTES + 1024
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z')


def _snapshot(receipt: Any) -> bytes:
    try:
        if not isinstance(receipt, Mapping) or set(receipt) != {'statement', 'signature'}:
            raise ValueError
        encoded = canonical_json_bytes(dict(receipt))
        if len(encoded) > MAX_RECEIPT_BYTES:
            raise ValueError
        return encoded
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError('token_progress_frame_invalid') from exc


def validate_progress_frame(value: Any, *, node_id: str) -> dict[str, Any]:
    if (not isinstance(value, dict)
            or set(value) != {'protocol', 'command_id', 'node_id', 'receipt'}
            or value.get('protocol') != PROGRESS_PROTOCOL
            or value.get('node_id') != node_id
            or any(type(value.get(k)) is not str or not _IDENTIFIER.fullmatch(value[k])
                   for k in ('command_id', 'node_id'))):
        raise ValueError('token_progress_frame_invalid')
    receipt = json.loads(_snapshot(value['receipt']))
    return {**value, 'receipt': receipt}


def progress_frame(*, command_id: str, node_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
    return validate_progress_frame(dict(protocol=PROGRESS_PROTOCOL, command_id=command_id,
                                       node_id=node_id, receipt=receipt), node_id=node_id)


@dataclass(frozen=True)
class VerifiedReceipt:
    sequence: int
    token_ids: tuple[int, ...]
    status: str
    error_code: str | None
    receipt_digest: str
    # Canonical bytes: downstream sealing must retain exactly what was verified.
    envelope_bytes: bytes


class ProgressCommandEnded(RuntimeError):
    """Ordinary command ended after all queued receipts; not receipt success."""


class ReceiptInbox:
    """One consumer, bounded nonblocking producer, absolute deadline.

    Overflow poisons only this stream and never silently drops a prefix. It does
    not stop remote compute: the owner still owes cancellation/reconciliation.
    close() discards queued tokens immediately; finish() allows already-arrived
    receipts to drain before reporting EOF/command completion without a terminal.
    """
    def __init__(self, *, cursor: ReceiptCursor, deadline_monotonic_s: float,
                 capacity: int = 64):
        if (not isinstance(cursor, ReceiptCursor) or type(capacity) is not int
                or not 1 <= capacity <= 64
                or type(deadline_monotonic_s) not in (float, int)
                or not math.isfinite(deadline_monotonic_s)):
            raise ValueError('token_progress_arguments_invalid')
        self._cursor = cursor
        self._deadline = min(deadline_monotonic_s, cursor.grant.deadline_monotonic_s)
        self._capacity = capacity
        self._condition = threading.Condition()
        self._reading = threading.Lock()
        self._queue: deque[bytes] = deque()
        self._error: str | None = None
        self._finished: str | None = None
        self._terminal = False

    def feed(self, receipt: Any) -> None:
        try:
            encoded = _snapshot(receipt)
        except ValueError:
            self.close('token_progress_frame_invalid')
            return
        with self._condition:
            if self._error is not None:
                return
            if self._terminal or self._finished is not None:
                self._error = 'token_progress_after_terminal'
                self._queue.clear()
            elif len(self._queue) >= self._capacity:
                self._error = 'token_progress_overflow'
                self._queue.clear()
            else:
                self._queue.append(encoded)
            self._condition.notify_all()

    def close(self, code: str = 'token_progress_closed') -> None:
        with self._condition:
            self._error = self._error or code
            self._queue.clear()
            self._condition.notify_all()

    def finish(self, code: str) -> None:
        with self._condition:
            self._finished = self._finished or code
            self._condition.notify_all()

    def _check(self) -> None:
        if self._error is not None:
            raise RuntimeError(self._error)
        if time.monotonic() >= self._deadline:
            self._error = 'command_deadline_exceeded'
            self._queue.clear()
            raise TimeoutError(self._error)
        if self._terminal:
            raise RuntimeError('token_progress_terminal')

    def read(self) -> VerifiedReceipt:
        # Fail rather than queue a second reader behind an unbounded caller.
        if not self._reading.acquire(blocking=False):
            raise RuntimeError('token_progress_concurrent_read')
        try:
            with self._condition:
                while True:
                    self._check()
                    if self._queue:
                        encoded = self._queue.popleft()
                        break
                    if self._finished is not None:
                        if self._finished == 'token_progress_missing_terminal':
                            raise ProgressCommandEnded(self._finished)
                        raise RuntimeError(self._finished)
                    self._condition.wait(timeout=max(0., self._deadline - time.monotonic()))
            envelope = json.loads(encoded)
            # Never execute caller predicates or signature validation on the
            # shared pipe-reader thread or while holding the producer lock.
            try:
                tokens = self._cursor.accept(envelope)
            except Exception:
                self.close('token_progress_invalid_receipt')
                raise
            with self._condition:
                self._check()  # cancellation/deadline may have overtaken verify
                body = envelope['statement']
                self._terminal = body['status'] != 'DECODING'
                if self._terminal and self._queue:
                    self._error = 'token_progress_after_terminal'
                    self._queue.clear()
                    raise RuntimeError(self._error)
                return VerifiedReceipt(body['sequence'], tokens, body['status'],
                                       body['error_code'],
                                       envelope['signature']['signed_statement_digest'], encoded)
        finally:
            self._reading.release()
