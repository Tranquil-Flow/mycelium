# SPDX-License-Identifier: AGPL-3.0-only
"""Reservation lease validation shared by path locking and adapters.

KV lease renewal on progress (A14 serve-2, f9a8d961 r1)
------------------------------------------------------
A path's reservation lease is issued once, at path build, and every stage
runtime refuses an operation of that path once its clock passes the lease
(``kv_lease_expired``).  The issued lease bounds the time to a path's *first*
operation at a stage.  After that, a stage runtime keeps the KV of a path
alive for as long as the owning request makes progress: every *applied*
continuation (a later prefill chunk, a decode step) renews the stage's own
local deadline to ``now + window``.  Abandoned work still expires: with no
applied operation for ``window`` seconds the KV is released and later work is
refused.

The renewal is local and needs no message: only an operation that passed the
relay's path admission (a manifest registered by the path's authenticated
owner) *and* every runtime binding check of the existing KV state (request,
path attempt, placement, position, sequence and the manifest-bound lease
value) renews, and it renews only that state.  So no party can extend a lease
without already holding the authority to execute that path's work.  A replay
of an applied operation, a refused operation or another path's operation never
renews, and one renewal is bounded by one window (never cumulative).
"""

from __future__ import annotations

import math
from typing import Any, Callable

from mycelium_router.contracts import PathHop, RouterConfig


#: The renewal window a stage runtime applies when its composer does not
#: scale it with the measured link: the Router's base reservation lease.
DEFAULT_KV_LEASE_RENEWAL_SECONDS = RouterConfig.reservation_lease_seconds


class ReservationLeaseError(ValueError):
   def __init__(self, code: str):
      self.code = code
      super().__init__(code)


def validate_hop_leases(
   hops: tuple[PathHop, ...],
   *,
   deployment_epoch: int,
   now: float,
) -> None:
   if any(
      hop.reservation_epoch not in {-1, deployment_epoch}
      for hop in hops
   ):
      raise ReservationLeaseError("deployment_epoch_mismatch")
   if any(hop.reservation_expires_at <= now for hop in hops):
      raise ReservationLeaseError("reservation_expired")


def kv_lease_deadline(state: Any) -> float:
   """The instant a path's stage-local KV expires: the later of the issued
   (manifest-bound) lease and its last progress renewal."""

   return max(
      float(state.lease_expires_at),
      float(getattr(state, "lease_renewed_until", float("-inf"))),
   )


def validated_renewal_source(
   source: float | Callable[[], float] | None,
) -> float | Callable[[], float] | None:
   """Validate a runtime's renewal window: seconds, a provider, or ``None`` (off)."""

   if source is None or callable(source):
      return source
   if (
      isinstance(source, bool)
      or not isinstance(source, (int, float))
      or not math.isfinite(float(source))
      or float(source) <= 0
   ):
      raise ValueError("invalid_kv_lease_renewal_seconds")
   return float(source)


def renewal_window_seconds(source: float | Callable[[], float] | None) -> float | None:
   """Current renewal window in seconds, or ``None`` when renewal is off.

   A provider that fails or returns an unusable value yields ``None`` for this
   operation: the lease is then simply not renewed (fail closed to expiry).
   """

   if source is None:
      return None
   try:
      value = source() if callable(source) else source
   except Exception:
      return None
   if (
      isinstance(value, bool)
      or not isinstance(value, (int, float))
      or not math.isfinite(float(value))
      or float(value) <= 0
   ):
      return None
   return float(value)


def renew_kv_lease(
   state: Any,
   *,
   now: float,
   source: float | Callable[[], float] | None,
) -> None:
   """Renew ``state``'s local KV deadline after one applied continuation."""

   window = renewal_window_seconds(source)
   if window is None:
      return
   try:
      current = float(now)
   except (TypeError, ValueError):
      return
   if not math.isfinite(current):
      return
   state.lease_renewed_until = max(
      float(getattr(state, "lease_renewed_until", float("-inf"))),
      current + window,
   )
