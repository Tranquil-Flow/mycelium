"""Progressive route construction over the acyclic model-stage graph."""

import math
from dataclasses import replace

from mycelium_router.contracts import (
   BranchDecision,
   DeviceState,
   ExecutionGraph,
   PathBuildState,
   PathHop,
   PathManifest,
   RequestContext,
   ReservationRequest,
)
from mycelium_router.leases import ReservationLeaseError, validate_hop_leases
from mycelium_router.scoring import RouteScorer
from mycelium_router.validation import validate_execution_graph, validate_manifest


class RoutingError(RuntimeError):
   def __init__(self, code: str, detail: str = ""):
      self.code = code
      self.detail = detail
      super().__init__(code if not detail else f"{code}: {detail}")


class RoutePolicy:
   def __init__(self, scorer: RouteScorer):
      self.scorer = scorer

   def choose_next(
      self,
      build: PathBuildState,
      states: dict[str, DeviceState],
      *,
      now: float,
   ) -> BranchDecision:
      prefix = tuple(hop.placement_id for hop in build.ordered_hops)
      candidates = self._complete_routes(build, prefix)
      scored: list[tuple[float, tuple[str, ...], object]] = []
      for route in candidates:
         score = self.scorer.score_route(
            build.request, build.graph, route, states, now=now
         )
         if not math.isinf(score.total_score):
            scored.append((score.total_score, route, score))
      if not scored:
         raise RoutingError("no_feasible_route")
      _, route, score = min(scored, key=lambda item: (item[0], item[1]))
      return BranchDecision(
         placement_id=route[len(prefix)],
         complete_route=route,
         score=score,
      )

   def _complete_routes(
      self,
      build: PathBuildState,
      prefix: tuple[str, ...],
   ) -> tuple[tuple[str, ...], ...]:
      graph = build.graph
      placement_map = {
         placement.placement_id: placement
         for stage in graph.stages
         for placement in stage.placements
      }
      stage_index = {
         placement.placement_id: index
         for index, stage in enumerate(graph.stages)
         for placement in stage.placements
      }
      outgoing: dict[str, list[tuple[str, str]]] = {}
      for edge in graph.edges:
         outgoing.setdefault(edge.from_placement_id, []).append(
            (edge.edge_id, edge.to_placement_id)
         )

      def allowed(placement_id: str) -> bool:
         placement = placement_map[placement_id]
         return (
            placement_id not in build.excluded_placements
            and placement.node_id not in build.excluded_devices
            and placement.lifecycle_state == "ACTIVE"
         )

      if prefix:
         if not all(allowed(item) for item in prefix):
            return ()
         expected_index = len(prefix) - 1
         if stage_index.get(prefix[-1]) != expected_index:
            return ()
         partials = [prefix]
      else:
         partials = [
            (placement.placement_id,)
            for placement in graph.stages[0].placements
            if allowed(placement.placement_id)
         ]

      while partials and len(partials[0]) < len(graph.stages):
         expanded: list[tuple[str, ...]] = []
         for partial in partials:
            current = partial[-1]
            for edge_id, destination in sorted(outgoing.get(current, [])):
               if edge_id in build.excluded_edges or not allowed(destination):
                  continue
               if stage_index[destination] != len(partial):
                  continue
               expanded.append(partial + (destination,))
         partials = expanded

      loopback_pairs = {
         (edge.from_placement_id, edge.to_placement_id)
         for edge in graph.loopback_edges
         if edge.edge_id not in build.excluded_edges
      }
      return tuple(
         route
         for route in partials
         if len(route) == len(graph.stages)
         and (route[-1], route[0]) in loopback_pairs
      )


class ProgressivePathBuilder:
   def __init__(self, *, policy, capacity, id_source):
      self.policy = policy
      self.capacity = capacity
      self.id_source = id_source

   def start(
      self,
      request: RequestContext,
      graph: ExecutionGraph,
      *,
      path_attempt: int,
      excluded_placements: frozenset[str] = frozenset(),
      excluded_edges: frozenset[str] = frozenset(),
      excluded_devices: frozenset[str] = frozenset(),
   ) -> PathBuildState:
      validate_execution_graph(graph)
      return PathBuildState(
         request=request,
         graph=graph,
         path_id=self.id_source.new("path"),
         path_attempt=path_attempt,
         excluded_placements=excluded_placements,
         excluded_edges=excluded_edges,
         excluded_devices=excluded_devices,
      )

   @staticmethod
   def is_complete(build: PathBuildState) -> bool:
      return len(build.ordered_hops) == len(build.graph.stages)

   def advance(
      self,
      build: PathBuildState,
      states: dict[str, DeviceState],
      *,
      now: float,
   ) -> PathBuildState:
      if self.is_complete(build):
         raise RoutingError("path_already_complete")
      current = build
      while True:
         try:
            decision = self.policy.choose_next(current, states, now=now)
         except RoutingError:
            self.abort(current)
            raise
         stage_index = len(current.ordered_hops)
         stage = current.graph.stages[stage_index]
         kv_bytes = (
            len(current.request.prompt_token_ids) + current.request.max_new_tokens
         ) * stage.stage_cost.kv_bytes_per_context_token
         reservation = self.capacity.reserve(
            ReservationRequest(
               request_id=current.request.request_id,
               path_id=current.path_id,
               path_attempt=current.path_attempt,
               placement_id=decision.placement_id,
               kv_bytes=kv_bytes,
               deployment_epoch=current.graph.deployment_epoch,
               lease_expires_at=(
                  now + self.policy.scorer.config.reservation_lease_seconds
               ),
            )
         )
         reservation_is_live = (
            reservation.accepted
            and reservation.deployment_epoch == current.graph.deployment_epoch
            and reservation.expires_at > now
         )
         if reservation_is_live:
            hop = PathHop(
               stage_id=stage.stage_id,
               placement_id=decision.placement_id,
               reservation_id=reservation.reservation_id,
               reservation_expires_at=reservation.expires_at,
               reservation_epoch=reservation.deployment_epoch,
            )
            return replace(current, ordered_hops=current.ordered_hops + (hop,))
         if reservation.accepted and reservation.reservation_id:
            self.capacity.release((reservation.reservation_id,))
         current = replace(
            current,
            excluded_placements=current.excluded_placements
            | frozenset({decision.placement_id}),
         )

   def abort(self, build: PathBuildState) -> None:
      reservation_ids = tuple(
         hop.reservation_id for hop in build.ordered_hops
      )
      if reservation_ids:
         self.capacity.release(reservation_ids)

   def lock(
      self,
      build: PathBuildState,
      *,
      now: float | None = None,
   ) -> PathManifest:
      if not self.is_complete(build):
         raise RoutingError("path_incomplete")
      lock_time = build.request.admitted_at if now is None else now
      try:
         validate_hop_leases(
            build.ordered_hops,
            deployment_epoch=build.graph.deployment_epoch,
            now=lock_time,
         )
      except ReservationLeaseError as error:
         self.abort(build)
         raise RoutingError(error.code) from error
      first = build.ordered_hops[0].placement_id
      last = build.ordered_hops[-1].placement_id
      loopback = next(
         (
            edge
            for edge in build.graph.loopback_edges
            if edge.from_placement_id == last
            and edge.to_placement_id == first
            and edge.edge_id not in build.excluded_edges
         ),
         None,
      )
      if loopback is None:
         raise RoutingError("no_legal_loopback")
      manifest = PathManifest(
         path_id=build.path_id,
         path_attempt=build.path_attempt,
         request_id=build.request.request_id,
         deployment_id=build.graph.deployment_id,
         deployment_epoch=build.graph.deployment_epoch,
         topology_version=build.graph.topology_version,
         manifest_digest=build.graph.manifest_digest,
         ordered_hops=build.ordered_hops,
         loopback_edge_id=loopback.edge_id,
      )
      validate_manifest(manifest, build.graph)
      synchronize = getattr(self.capacity, "synchronize_build", None)
      if synchronize is not None:
         synchronized = synchronize(build)
         if not synchronized.accepted:
            self.abort(build)
            raise RoutingError(
               "path_reservation_sync_rejected",
               synchronized.reason,
            )
      commit = self.capacity.commit(
         tuple(hop.reservation_id for hop in build.ordered_hops),
         deployment_epoch=build.graph.deployment_epoch,
      )
      if not commit.accepted:
         self.abort(build)
         raise RoutingError("path_commit_rejected", commit.reason)
      return manifest
