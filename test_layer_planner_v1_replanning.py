import copy
import json
import math
import unittest

from mycelium_layer_planner.planner import plan_snapshot
from mycelium_layer_planner.replanning import (
    ReplanAssessment,
    ReplanOutcome,
    TopologyEvent,
    assess_topology_event,
    dumps_replan_outcome,
    replan_for_event,
    replan_outcome_to_dict,
)
from mycelium_layer_planner.validation import validate_route_plan
from test_layer_planner_v1_planner import snapshot


def fast_join_snapshot():
    data = snapshot(6)
    data["nodes"].append(
        {
            "node_id": "n6",
            "prefill_ms_per_layer_token": 0.0001,
            "decode_ms_per_layer_token": 0.0001,
            "fast_memory_bytes": 100_000_000,
            "total_memory_bytes": 200_000_000,
            "memory_bandwidth_Bps": 2_000_000_000,
            "spill_bandwidth_Bps": 2_000_000_000,
        }
    )
    for index in range(6):
        other = f"n{index}"
        data["links"].extend(
            [
                {
                    "src": "n6",
                    "dst": other,
                    "rtt_ms": 0.2,
                    "jitter_ms": 0.01,
                    "bandwidth_Bps": 1_000_000_000,
                },
                {
                    "src": other,
                    "dst": "n6",
                    "rtt_ms": 0.2,
                    "jitter_ms": 0.01,
                    "bandwidth_Bps": 1_000_000_000,
                },
            ]
        )
    return data


class ReplanningTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = snapshot(6)
        self.plan = plan_snapshot(self.snapshot)

    def event(self, kind, *, nodes=(), edges=(), generation=2):
        return TopologyEvent(
            event_id=f"test-{kind}",
            snapshot_generation=generation,
            kind=kind,
            node_ids=nodes,
            edges=edges,
        )

    def test_public_api_exports_replanner_contracts(self):
        import mycelium_layer_planner as public

        expected = {
            "TopologyEvent",
            "ReplanAssessment",
            "ReplanOutcome",
            "assess_topology_event",
            "dumps_replan_outcome",
            "replan_for_event",
            "replan_outcome_to_dict",
        }
        self.assertTrue(expected.issubset(set(public.__all__)))
        for name in expected:
            self.assertIsNotNone(getattr(public, name))

    def test_replica_dropout_keeps_surviving_track_intent(self):
        assessment = assess_topology_event(
            self.plan,
            self.event("device_unavailable", nodes=("n4",)),
        )
        self.assertEqual(assessment.action, "existing_track_intent")
        self.assertEqual(assessment.urgency, "immediate")
        self.assertEqual(assessment.surviving_track_ids, ("track-000", "track-002"))
        self.assertTrue(assessment.external_readiness_required)
        self.assertEqual(assessment.escalation_order, ())

    def test_unknown_unavailable_node_is_no_action(self):
        assessment = assess_topology_event(
            self.plan,
            self.event("device_unavailable", nodes=("not-in-plan",)),
        )
        self.assertEqual(assessment.action, "no_action")
        self.assertEqual(assessment.urgency, "none")
        self.assertFalse(assessment.external_readiness_required)

    def test_topology_event_rejects_malformed_inputs(self):
        invalid = [
            {"event_id": "", "snapshot_generation": 1, "kind": "device_unavailable", "node_ids": ("n1",)},
            {"event_id": "x", "snapshot_generation": -1, "kind": "device_unavailable", "node_ids": ("n1",)},
            {"event_id": "x", "snapshot_generation": 1, "kind": "other", "node_ids": ("n1",)},
            {"event_id": "x", "snapshot_generation": 1, "kind": "device_unavailable"},
            {"event_id": "x", "snapshot_generation": 1, "kind": "edge_unavailable", "edges": (("n1", "n1"),)},
            {"event_id": "x", "snapshot_generation": 1, "kind": "edge_unavailable", "edges": ("ab",)},
            {"event_id": "x", "snapshot_generation": 1, "kind": "device_unavailable", "node_ids": ("n1",), "edges": (("n1", "n2"),)},
            {"event_id": "x", "snapshot_generation": 1, "kind": "edge_unavailable", "node_ids": ("n1",), "edges": (("n1", "n2"),)},
            {"event_id": "x", "snapshot_generation": 1, "kind": "device_joined"},
            {"event_id": "x", "snapshot_generation": 1, "kind": "device_joined", "node_ids": ("n6",), "edges": (("n1", "n6"),)},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TopologyEvent(**kwargs)

    def test_unique_interior_stage_prefers_standby_then_full_replan(self):
        assessment = assess_topology_event(
            self.plan,
            self.event("device_unavailable", nodes=("n1",)),
        )
        self.assertEqual(assessment.action, "full_replan")
        self.assertEqual(assessment.affected_group_ids, ("stage-001",))
        self.assertEqual(
            assessment.escalation_order,
            ("successor_standby_candidate", "full_replan"),
        )

    def test_first_stage_loss_requires_full_replan_without_standby_claim(self):
        assessment = assess_topology_event(
            self.plan,
            self.event("device_unavailable", nodes=("n0",)),
        )
        self.assertEqual(assessment.action, "full_replan")
        self.assertEqual(assessment.affected_group_ids, ("stage-000",))
        self.assertEqual(assessment.escalation_order, ("full_replan",))

    def test_directed_edge_failure_preserves_only_unaffected_tracks(self):
        assessment = assess_topology_event(
            self.plan,
            self.event("edge_unavailable", edges=(("n2", "n3"),)),
        )
        self.assertEqual(assessment.action, "existing_track_intent")
        self.assertEqual(assessment.surviving_track_ids, ("track-001", "track-002"))

        reverse = assess_topology_event(
            self.plan,
            self.event("edge_unavailable", edges=(("n3", "n2"),)),
        )
        self.assertEqual(reverse.action, "no_action")

    def test_failed_node_is_absent_from_candidate_plan(self):
        outcome = replan_for_event(
            self.plan,
            self.snapshot,
            self.event("device_unavailable", nodes=("n1",)),
        )
        self.assertIsInstance(outcome, ReplanOutcome)
        self.assertIsNotNone(outcome.candidate_plan)
        validate_route_plan(outcome.candidate_plan)
        self.assertNotIn("n1", {placement.node_id for placement in outcome.candidate_plan.placements})
        self.assertEqual(outcome.candidate_plan.handoff_state, "placement_intent_only")
        self.assertEqual(
            outcome.recommendation,
            "prefer_route_ready_successor_standby_else_provision_candidate",
        )

    def test_replan_does_not_mutate_snapshot_or_previous_plan(self):
        before_snapshot = copy.deepcopy(self.snapshot)
        before_plan = self.plan
        replan_for_event(
            self.plan,
            self.snapshot,
            self.event("device_unavailable", nodes=("n1",)),
        )
        self.assertEqual(self.snapshot, before_snapshot)
        self.assertEqual(self.plan, before_plan)

    def test_join_is_deferred_and_hysteresis_gated(self):
        joined = fast_join_snapshot()
        event = self.event("device_joined", nodes=("n6",))
        beneficial = replan_for_event(
            self.plan,
            joined,
            event,
            min_capacity_gain_fraction=0.05,
        )
        self.assertEqual(beneficial.assessment.action, "candidate_replan")
        self.assertEqual(beneficial.assessment.urgency, "deferred")
        self.assertEqual(beneficial.recommendation, "provision_candidate")
        self.assertGreater(beneficial.capacity_gain_fraction, 0.05)

        gated = replan_for_event(
            self.plan,
            joined,
            event,
            min_capacity_gain_fraction=2.0,
        )
        self.assertEqual(gated.recommendation, "retain_current_plan")
        self.assertIsNotNone(gated.candidate_plan)

    def test_joined_node_must_exist_in_new_snapshot(self):
        with self.assertRaises(ValueError):
            replan_for_event(
                self.plan,
                self.snapshot,
                self.event("device_joined", nodes=("n6",)),
            )

    def test_no_feasible_candidate_is_typed_without_stale_fallback(self):
        outcome = replan_for_event(
            self.plan,
            self.snapshot,
            self.event(
                "device_unavailable",
                nodes=("n1", "n2", "n3", "n4", "n5"),
            ),
        )
        self.assertEqual(outcome.recommendation, "no_viable_plan")
        self.assertIsNone(outcome.candidate_plan)
        self.assertIsNone(outcome.candidate_plan_digest)
        self.assertNotIn("fallback", outcome.reason.lower())

    def test_replan_outcome_serialization_is_canonical_and_complete(self):
        outcome = replan_for_event(
            self.plan,
            self.snapshot,
            self.event("device_unavailable", nodes=("n1",), generation=42),
        )
        payload = replan_outcome_to_dict(outcome)
        text = dumps_replan_outcome(outcome)
        self.assertEqual(json.loads(text), payload)
        self.assertEqual(text, dumps_replan_outcome(outcome))
        self.assertEqual(payload["protocol"], "mycelium.layer_replan.v1")
        self.assertEqual(payload["event"]["snapshot_generation"], 42)
        self.assertEqual(
            payload["assessment"]["protocol"],
            "mycelium.layer_replan_assessment.v1",
        )
        self.assertEqual(payload["candidate_plan"]["protocol"], "mycelium.route_plan.v2")
        self.assertEqual(payload["handoff_state"], "placement_intent_only")

    def test_replan_outcome_is_deterministic(self):
        event = self.event("device_unavailable", nodes=("n1",))
        first = replan_for_event(self.plan, self.snapshot, event)
        second = replan_for_event(self.plan, self.snapshot, event)
        self.assertEqual(first, second)

    def test_invalid_hysteresis_and_nonfinite_capacity_are_rejected(self):
        event = self.event("device_joined", nodes=("n6",))
        joined = fast_join_snapshot()
        for threshold in (-0.1, math.nan, math.inf):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                replan_for_event(
                    self.plan,
                    joined,
                    event,
                    min_capacity_gain_fraction=threshold,
                )

        self.plan.metrics["replicated_request_capacity_rps"] = math.nan
        with self.assertRaises(ValueError):
            replan_for_event(self.plan, joined, event)

    def test_contracts_reject_overclaiming_values(self):
        with self.assertRaises(ValueError):
            ReplanAssessment(
                action="failover_ready",
                urgency="immediate",
                surviving_track_ids=(),
                affected_group_ids=(),
                escalation_order=(),
                external_readiness_required=False,
                reason="invalid",
            )
        with self.assertRaises(ValueError):
            ReplanAssessment(
                action="full_replan",
                urgency="immediate",
                surviving_track_ids=(),
                affected_group_ids=("stage-001",),
                escalation_order=("runtime_ready",),
                external_readiness_required=True,
                reason="invalid",
            )
        outcome = replan_for_event(
            self.plan,
            self.snapshot,
            self.event("device_unavailable", nodes=("n4",)),
        )
        lowered = repr(outcome).lower()
        self.assertNotIn("runtime_ready", lowered)
        self.assertNotIn("weights_loaded", lowered)
        self.assertNotIn("failover_ready", lowered)
        self.assertNotIn("activated=true", lowered)


if __name__ == "__main__":
    unittest.main()
