# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV10 owner side: use the existing A7 / A6 recovery for a member that is leaving smoothly.

When a member tells the owner (signed ``planning`` notice, see ``member_withdrawal``) which requests are still running on
it, the owner does not wait for loss detection: for each request that really runs through that member it takes the same
decision the router takes when it observes the placement lost, only earlier and with the member still alive:

1. a standby covers the member's source placement -> the existing A7 pair ``accept_source_lost`` /
   ``cut_over_lost_source`` (fenced KV-successor cutover; it falls back to the A6 replay itself when the standby is not
   eligible, and the answer here says which one it really was - never a KV continuity that did not happen);
2. otherwise -> the existing A6 seam ``report_lost_source`` (full-context replay on a replacement path);
3. anything the owner cannot act on early (a request still prefilling, a router that errors) -> ``loss_detection``:
   the member keeps draining within its stall bound, exactly as before.

This module only calls those public seams; it changes no A6/A7 behaviour.  The owner never takes the member's word for
what is running: a request that is unknown, finished or not routed through that member's placements is
``not_in_flight`` and is left alone.
"""
from __future__ import annotations

from typing import Any, Iterable

from .member_withdrawal import MAX_IN_FLIGHT, valid_request_id

REASON = 'member_leaving'
_TERMINAL = frozenset({'COMPLETED', 'FAILED', 'CANCELLED'})


class LeaveHandoff:
    def __init__(self, router: Any, wiring: Any = None) -> None:
        self.router = router            # the entry Router (``router.entry`` is the RouterEntry)
        self.wiring = wiring            # the A7NodeWiring of the entry node, when A7 is configured

    @property
    def _entry(self) -> Any:
        return getattr(self.router, 'entry', self.router)

    def plan(self, node_id: str, generation: int, request_ids: Iterable[str]) -> dict[str, str]:
        decisions: dict[str, str] = {}
        for request_id in list(request_ids)[:MAX_IN_FLIGHT]:
            if not valid_request_id(request_id):
                continue
            try:
                decisions[request_id] = self._decide(node_id, request_id)
            except Exception:
                decisions[request_id] = 'loss_detection'
        return decisions

    # -- one request -------------------------------------------------------------------------------------------------------
    def _decide(self, node_id: str, request_id: str) -> str:
        entry = self._entry
        try:
            status = entry.request_status(request_id)
        except KeyError:
            return 'not_in_flight'
        if status in _TERMINAL:
            return 'not_in_flight'
        try:
            record = entry.get_request(request_id)
        except KeyError:
            return 'loss_detection'        # still a pending prefill: nothing to cut over yet
        if node_id == getattr(entry, 'node_id', None):
            return 'loss_detection'        # the entry itself leaving is not recoverable here
        on_member = {placement.placement_id for stage in record.graph.stages for placement in stage.placements
                     if placement.node_id == node_id}
        hops = [hop.placement_id for hop in record.manifest.ordered_hops if hop.placement_id in on_member]
        if not hops:
            return 'not_in_flight'
        decision = self._a7(node_id, request_id, record)
        if decision is not None:
            return decision
        return 'a6_replay' if entry.report_lost_source(request_id, hops[0], reason=REASON) else 'loss_detection'

    def _a7(self, node_id: str, request_id: str, record: Any) -> str | None:
        wiring = self.wiring
        controller = getattr(wiring, 'controller', None)
        if controller is None or controller.covered_source_placement(request_id, node_id) is None:
            return None
        claim = wiring.accept_source_lost(request_id, node_id)
        if claim.get('accepted') is not True:
            return 'not_in_flight' if claim.get('reason') == 'request_terminal' else None
        if claim.get('cutover_pending') is True:
            wiring.cut_over_lost_source(self.router, request_id, node_id)
        outcome = (getattr(record, 'kv_successor_cutover', None) or {}).get('outcome')
        if outcome == 'fenced_kv_successor':
            return 'a7_kv_standby'
        if outcome == 'full_context_replay':
            return 'a6_replay'
        return 'loss_detection'
