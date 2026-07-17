"""Reservation lease validation shared by path locking and adapters."""

from mycelium_router.contracts import PathHop


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
