# SPDX-License-Identifier: AGPL-3.0-only
"""A7: one Router runtime that also hosts dedicated STANDBY placement ports.

A node that serves an ordinary placement and holds a standby for another stage
of the same route keeps two runtime ports: its ordinary port and a standby port
bound only to the STANDBY placement (KV is keyed by path, so a successor path
that crosses both placements on this node needs two ports).  The Router owns a
single runtime; this wrapper routes every work item by its placement to the
port that binds it and fans path-scoped cleanup out to every port.

Ordinary placements keep their exact behavior (including A10 batching).  The
standby port never batches and never prefills: its own guards refuse that.
Evidence (``kv_snapshot``) merges both ports; standby states are keyed
``"<path_id>@standby"`` so the two ports' records never collide.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mycelium_router.contracts import RuntimeBatch


class StandbyDispatchRuntime:
   def __init__(self, primary: Any, standby_ports: Mapping[str, Any]) -> None:
      if not standby_ports:
         raise ValueError("standby_ports_required")
      self.primary = primary
      self._standby = dict(standby_ports)
      for placement_id, port in self._standby.items():
         if placement_id not in getattr(port, "standby_placement_ids", frozenset()):
            raise ValueError("standby_port_binding_mismatch")
      if getattr(primary, "decode_mode", None) != "stage_local_kv":
         raise ValueError("standby_requires_stage_local_kv")
      self.decode_mode = primary.decode_mode

   @property
   def standby_ports(self) -> dict[str, Any]:
      return dict(self._standby)

   def port_for(self, placement_id: str) -> Any:
      return self._standby.get(placement_id, self.primary)

   def _ports(self) -> tuple[Any, ...]:
      unique: list[Any] = [self.primary]
      for port in self._standby.values():
         if all(port is not seen for seen in unique):
            unique.append(port)
      return tuple(unique)

   # -- execution -----------------------------------------------------------------

   def execute(self, item: Any) -> Any:
      return self.port_for(getattr(item, "placement_id", "")).execute(item)

   def execute_batch(self, batch: Any) -> Any:
      if isinstance(batch, RuntimeBatch):
         placements = {item.placement_id for item in batch.items}
         if placements & set(self._standby):
            return tuple(self.execute(item) for item in batch.items)
      return self.primary.execute_batch(batch)

   def offer_batch_member(self, item: Any, *, member_ref: str) -> Any:
      if getattr(item, "placement_id", None) in self._standby:
         # Standby work is never batched; the relay falls back to sequential.
         raise ValueError("standby_placement_not_batched")
      return self.primary.offer_batch_member(item, member_ref=member_ref)

   def cancel(self, path_id: str) -> None:
      errors: list[BaseException] = []
      for port in self._ports():
         try:
            port.cancel(path_id)
         except BaseException as exc:  # every port is attempted exactly once
            errors.append(exc)
      if errors:
         raise errors[0]

   def close(self, *, reason: str = "worker_shutdown") -> None:
      for port in self._ports():
         close = getattr(port, "close", None)
         if callable(close):
            close(reason=reason)

   # -- evidence ------------------------------------------------------------------

   def kv_subject_clean(self, request_id: str, path_id: str, path_attempt: int) -> bool:
      for port in self._ports():
         probe = getattr(port, "kv_subject_clean", None)
         if callable(probe) and not probe(request_id, path_id, path_attempt):
            return False
      return True

   def _merge(self, primary: dict[str, Any] | None, standbys: list[dict[str, Any] | None]) -> dict[str, Any] | None:
      if primary is None or any(snapshot is None for snapshot in standbys):
         return None
      merged = dict(primary)
      states = dict(primary.get("states") or {})
      for snapshot in standbys:
         assert snapshot is not None
         for path_id, state in (snapshot.get("states") or {}).items():
            states[f"{path_id}@standby"] = state
         for key, value in snapshot.items():
            if key == "states":
               continue
            if type(value) is int and type(merged.get(key)) is int:
               merged[key] = merged[key] + value
      merged["states"] = states
      merged["standby_placement_count"] = len(self._standby)
      return merged

   def kv_snapshot(self) -> dict[str, Any]:
      standbys = [port.kv_snapshot() for port in self._ports()[1:]]
      merged = self._merge(self.primary.kv_snapshot(), standbys)
      assert merged is not None
      return merged

   def kv_snapshot_nonblocking(self) -> dict[str, Any] | None:
      def take(port: Any) -> dict[str, Any] | None:
         probe = getattr(port, "kv_snapshot_nonblocking", None)
         return probe() if callable(probe) else port.kv_snapshot()

      return self._merge(take(self.primary), [take(port) for port in self._ports()[1:]])

   # -- A7 KV surface ---------------------------------------------------------------

   def export_kv_checkpoint(self, path_id: str, *, from_position: int = 0) -> bytes:
      return self.primary.export_kv_checkpoint(path_id, from_position=from_position)

   def import_kv_checkpoint(self, payload: bytes, *, placement_id: str, **kwargs: Any) -> dict[str, Any]:
      return self.port_for(placement_id).import_kv_checkpoint(payload, placement_id=placement_id, **kwargs)

   def rekey_kv_path(self, old_path_id: str, **kwargs: Any) -> dict[str, Any]:
      return self.primary.rekey_kv_path(old_path_id, **kwargs)

   def __getattr__(self, name: str) -> Any:
      # Everything else (A10 surface, counters, identity) is the ordinary port's.
      if name in {"primary", "_standby"}:
         raise AttributeError(name)
      return getattr(self.primary, name)
