"""Pure route-cost model for prefill and decode traffic."""

import math

from mycelium_router.contracts import (
   DeviceState,
   ExecutionGraph,
   RequestContext,
   RouterConfig,
   ScoreBreakdown,
)


class RouteScorer:
   def __init__(self, config: RouterConfig):
      self.config = config

   def score_route(
      self,
      request: RequestContext,
      graph: ExecutionGraph,
      placement_ids: tuple[str, ...],
      states: dict[str, DeviceState],
      *,
      now: float,
   ) -> ScoreBreakdown:
      placement_map = {
         placement.placement_id: (stage, placement)
         for stage in graph.stages
         for placement in stage.placements
      }
      if len(placement_ids) != len(graph.stages):
         return self._infeasible()
      try:
         stage_placements = [placement_map[item] for item in placement_ids]
      except KeyError:
         return self._infeasible()

      legal_edges = {
         (edge.from_placement_id, edge.to_placement_id): edge
         for edge in graph.edges
      }
      for left, right in zip(placement_ids, placement_ids[1:]):
         if (left, right) not in legal_edges:
            return self._infeasible()
      loopback = next(
         (
            edge
            for edge in graph.loopback_edges
            if edge.from_placement_id == placement_ids[-1]
            and edge.to_placement_id == placement_ids[0]
         ),
         None,
      )
      if loopback is None:
         return self._infeasible()

      ttft_ms = 0.0
      tpot_ms = 0.0
      prefill_transfer_ms = 0.0
      decode_transfer_ms = 0.0
      confidence_values: list[float] = []
      fallback_nodes: set[str] = set()

      for stage, placement in stage_placements:
         state = states.get(placement.node_id)
         if state is None or state.availability != "ALIVE":
            return self._infeasible()
         projected_age = max(0.0, now - state.last_updated) + ttft_ms / 1000.0
         confidence = self._confidence(projected_age)
         confidence_values.append(confidence)
         use_fallback = confidence < self.config.minimum_confidence
         if use_fallback:
            fallback_nodes.add(placement.node_id)
         free_fraction = (
            self.config.conservative_compute_fraction
            if use_fallback
            else max(0.001, min(1.0, state.free_compute_fraction))
         )
         queue_depth = (
            self.config.conservative_queue_depth
            if use_fallback
            else max(0, state.pending_hop_queue_depth)
         )
         compute_rate = state.compute_units_per_second * free_fraction
         if compute_rate <= 0:
            return self._infeasible()
         prefill_compute_ms = (
            stage.stage_cost.prefill_work_units_per_prompt_token
            * len(request.prompt_token_ids)
            / compute_rate
            * 1000.0
         )
         decode_compute_ms = (
            stage.stage_cost.decode_work_units_per_token
            / compute_rate
            * 1000.0
         )
         ttft_ms += prefill_compute_ms + queue_depth * decode_compute_ms
         tpot_ms += decode_compute_ms + queue_depth * decode_compute_ms

      prefill_payload_bytes = (
         len(request.prompt_token_ids) * graph.hidden_size * graph.activation_bytes
      )
      decode_payload_bytes = graph.hidden_size * graph.activation_bytes
      token_payload_bytes = graph.token_envelope_bytes
      for left_id, right_id in zip(placement_ids, placement_ids[1:]):
         left = placement_map[left_id][1]
         right = placement_map[right_id][1]
         prefill_link = self._transfer_ms(
            left.node_id,
            right.node_id,
            prefill_payload_bytes,
            states,
         )
         decode_link = self._transfer_ms(
            left.node_id,
            right.node_id,
            decode_payload_bytes,
            states,
         )
         prefill_transfer_ms += prefill_link
         decode_transfer_ms += decode_link
      final_node = placement_map[placement_ids[-1]][1].node_id
      first_node = placement_map[placement_ids[0]][1].node_id
      decode_transfer_ms += self._transfer_ms(
         final_node,
         first_node,
         token_payload_bytes,
         states,
      )
      ttft_ms += prefill_transfer_ms
      tpot_ms += decode_transfer_ms
      alpha = self.config.alpha_for(request.qos_class)
      total = alpha * (ttft_ms / request.target_ttft_ms) + (1.0 - alpha) * (
         tpot_ms / request.target_tpot_ms
      )
      return ScoreBreakdown(
         ttft_ms=ttft_ms,
         tpot_ms=tpot_ms,
         prefill_transfer_ms=prefill_transfer_ms,
         decode_transfer_ms=decode_transfer_ms,
         total_score=total,
         confidence=min(confidence_values, default=0.0),
         fallback_nodes=tuple(sorted(fallback_nodes)),
      )

   def _confidence(self, projected_age_seconds: float) -> float:
      half_life = max(0.001, self.config.confidence_half_life_seconds)
      return math.exp(-math.log(2.0) * projected_age_seconds / half_life)

   def _transfer_ms(
      self,
      source_node: str,
      destination_node: str,
      payload_bytes: int,
      states: dict[str, DeviceState],
   ) -> float:
      if source_node == destination_node:
         return 0.0
      source = states.get(source_node)
      if source is None:
         return math.inf
      rtt_ms = source.neighbor_rtt_ms.get(
         destination_node, self.config.default_rtt_ms
      )
      bandwidth = source.neighbor_bandwidth_bytes_per_second.get(
         destination_node, self.config.default_bandwidth_bytes_per_second
      )
      if bandwidth <= 0:
         return math.inf
      return rtt_ms / 2.0 + payload_bytes / bandwidth * 1000.0

   @staticmethod
   def _infeasible() -> ScoreBreakdown:
      return ScoreBreakdown(
         ttft_ms=math.inf,
         tpot_ms=math.inf,
         prefill_transfer_ms=math.inf,
         decode_transfer_ms=math.inf,
         total_score=math.inf,
         confidence=0.0,
         fallback_nodes=(),
      )
