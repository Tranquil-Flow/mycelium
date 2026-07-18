from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from mycelium_gateway.asgi import ObservatoryASGIApplication
from mycelium_gateway.init import build_semantic_observatory_gateway
from mycelium_gateway.semantic import (
    OBSERVATORY_EVENT_PROTOCOL,
    OBSERVATORY_SNAPSHOT_PROTOCOL,
    ObservatoryOwnerError,
    SemanticValidationError,
    StaleSemanticSnapshotError,
    UnsupportedSemanticProtocolError,
    decode_observatory_event,
    decode_observatory_snapshot,
    qualify_observatory_snapshot,
)


NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
CANARY = "PHASE9_PRIVACY_CANARY_DO_NOT_PUBLISH"
DIGEST_A = "sha256:" + ("a" * 64)
DIGEST_B = "sha256:" + ("b" * 64)


def binding() -> dict[str, object]:
    return {
        "deployment": {"id": "deployment-alpha", "epoch": 7},
        "model": {
            "id": "mycelium-model",
            "revision": "commit-abc123",
            "manifest_digest": DIGEST_A,
            "num_layers": 4,
        },
        "route": {
            "id": "route-primary",
            "generation": 11,
            "digest": DIGEST_B,
            "assignments": [
                {
                    "id": "assignment-0",
                    "peer_id": "peer-alpha",
                    "start_layer": 0,
                    "end_layer_exclusive": 2,
                },
                {
                    "id": "assignment-1",
                    "peer_id": "peer-beta",
                    "start_layer": 2,
                    "end_layer_exclusive": 4,
                },
            ],
        },
    }


def freshness(
    observed_at: str = "2026-07-18T11:59:00Z",
    valid_until: str = "2026-07-18T12:05:00Z",
) -> dict[str, str]:
    return {"observed_at": observed_at, "valid_until": valid_until}


def provenance(kind: str, producer: str) -> dict[str, str]:
    return {"kind": kind, "producer": producer}


def semantic_snapshot() -> dict[str, object]:
    exact_binding = binding()
    return {
        "protocol": OBSERVATORY_SNAPSHOT_PROTOCOL,
        "snapshot_id": "snapshot-0001",
        "freshness": freshness(),
        "binding": exact_binding,
        "claims": [
            {
                "id": "claim-deployment",
                "scope": {"kind": "deployment", "id": "deployment-alpha"},
                "statement": "deployment_bound",
                "value": "confirmed",
                "freshness": freshness(),
                "provenance": provenance("gateway_projection", "mycelium_gateway"),
            },
            {
                "id": "claim-model",
                "scope": {"kind": "model", "id": "mycelium-model"},
                "statement": "model_bound",
                "value": "confirmed",
                "freshness": freshness(),
                "provenance": provenance("provisioning_audit", "mycelium_provisioning"),
            },
            {
                "id": "claim-assignment-0",
                "scope": {"kind": "assignment", "id": "assignment-0"},
                "statement": "assignment_ready",
                "value": "confirmed",
                "freshness": freshness(),
                "provenance": provenance("provisioning_audit", "mycelium_provisioning"),
            },
            {
                "id": "claim-assignment-1",
                "scope": {"kind": "assignment", "id": "assignment-1"},
                "statement": "assignment_ready",
                "value": "confirmed",
                "freshness": freshness(),
                "provenance": provenance("provisioning_audit", "mycelium_provisioning"),
            },
            {
                "id": "claim-challenge",
                "scope": {"kind": "route", "id": "route-primary"},
                "statement": "route_challenge_succeeded",
                "value": "confirmed",
                "freshness": freshness(),
                "provenance": provenance("route_challenge", "mycelium_router"),
            },
            {
                "id": "claim-request",
                "scope": {"kind": "request", "id": "request-observation-1"},
                "statement": "request_lifecycle_observed",
                "value": "confirmed",
                "freshness": freshness(),
                "provenance": provenance("router_runtime", "mycelium_router"),
            },
        ],
        "conflicts": [],
        "route_challenge": {
            "id": "challenge-0001",
            "status": "succeeded",
            "freshness": freshness(),
            "binding": deepcopy(exact_binding),
            "provenance": provenance("route_challenge", "mycelium_router"),
        },
        "request_lifecycle": {
            "request_id": "request-observation-1",
            "state": "completed",
            "path_attempt": 1,
            "freshness": freshness(),
            "binding": deepcopy(exact_binding),
            "provenance": provenance("router_runtime", "mycelium_router"),
        },
        "provenance": provenance("gateway_projection", "mycelium_gateway"),
    }


def semantic_event(generation: int = 1) -> dict[str, object]:
    return {
        "protocol": OBSERVATORY_EVENT_PROTOCOL,
        "generation": generation,
        "snapshot": semantic_snapshot(),
    }


class SemanticContractTests(unittest.TestCase):
    def test_decodes_exact_snapshot_and_event_contracts_without_aliasing_input(self):
        candidate = semantic_snapshot()
        decoded = decode_observatory_snapshot(candidate)
        event = decode_observatory_event(semantic_event(9))

        candidate["binding"]["deployment"]["id"] = "mutated"
        self.assertEqual(decoded["binding"]["deployment"]["id"], "deployment-alpha")
        self.assertEqual(event["protocol"], OBSERVATORY_EVENT_PROTOCOL)
        self.assertEqual(event["generation"], 9)
        self.assertEqual(event["snapshot"], decoded)

    def test_rejects_unknown_major_and_extra_fields_fail_closed(self):
        snapshot_v2 = semantic_snapshot()
        snapshot_v2["protocol"] = "mycelium.observatory.snapshot.v2"
        event_v10 = semantic_event()
        event_v10["protocol"] = "mycelium.observatory.event.v10"
        extra = semantic_snapshot()
        extra["debug_note"] = CANARY

        with self.assertRaises(UnsupportedSemanticProtocolError):
            decode_observatory_snapshot(snapshot_v2)
        with self.assertRaises(UnsupportedSemanticProtocolError):
            decode_observatory_event(event_v10)
        with self.assertRaises(SemanticValidationError):
            decode_observatory_snapshot(extra)

    def test_rejects_every_forbidden_privacy_category_and_network_or_path_identifiers(self):
        forbidden_fields = (
            "prompt",
            "token_ids",
            "token_content",
            "activations",
            "tensor",
            "weights",
            "credentials",
            "raw_endpoint",
            "raw_router_frame",
        )
        for field in forbidden_fields:
            with self.subTest(field=field):
                candidate = semantic_snapshot()
                candidate[field] = CANARY
                with self.assertRaises(SemanticValidationError):
                    decode_observatory_snapshot(candidate)

        for peer_id in ("127.0.0.1", "2001:db8::1", "https://peer.invalid", "/private/socket"):
            with self.subTest(peer_id=peer_id):
                candidate = semantic_snapshot()
                candidate["binding"]["route"]["assignments"][0]["peer_id"] = peer_id
                candidate["route_challenge"]["binding"] = deepcopy(candidate["binding"])
                candidate["request_lifecycle"]["binding"] = deepcopy(candidate["binding"])
                with self.assertRaises(SemanticValidationError):
                    decode_observatory_snapshot(candidate)

    def test_requires_exact_challenge_and_request_binding(self):
        for field_path in (
            ("deployment", "id"),
            ("deployment", "epoch"),
            ("model", "revision"),
            ("model", "manifest_digest"),
            ("route", "generation"),
            ("route", "digest"),
        ):
            with self.subTest(field_path=field_path):
                candidate = semantic_snapshot()
                container = candidate["request_lifecycle"]["binding"][field_path[0]]
                original = container[field_path[1]]
                container[field_path[1]] = original + 1 if isinstance(original, int) else f"{original}-other"
                with self.assertRaises(SemanticValidationError):
                    decode_observatory_snapshot(candidate)

        candidate = semantic_snapshot()
        candidate["route_challenge"]["binding"]["route"]["assignments"][0][
            "end_layer_exclusive"
        ] = 1
        with self.assertRaises(SemanticValidationError):
            decode_observatory_snapshot(candidate)

        wrong_claim_provenance = semantic_snapshot()
        wrong_claim_provenance["claims"][-1]["provenance"] = provenance(
            "gateway_projection", "mycelium_gateway"
        )
        with self.assertRaises(SemanticValidationError):
            decode_observatory_snapshot(wrong_claim_provenance)

    def test_validates_claim_scopes_assignment_coverage_and_conflict_references(self):
        bad_scope = semantic_snapshot()
        bad_scope["claims"][2]["scope"]["id"] = "assignment-missing"
        with self.assertRaises(SemanticValidationError):
            decode_observatory_snapshot(bad_scope)

        gap = semantic_snapshot()
        gap["binding"]["route"]["assignments"][1]["start_layer"] = 3
        gap["route_challenge"]["binding"] = deepcopy(gap["binding"])
        gap["request_lifecycle"]["binding"] = deepcopy(gap["binding"])
        with self.assertRaises(SemanticValidationError):
            decode_observatory_snapshot(gap)

        cross_scope_conflict = semantic_snapshot()
        cross_scope_conflict["conflicts"] = [
            {
                "claim_ids": ["claim-challenge", "claim-request"],
                "scope": {"kind": "route", "id": "route-primary"},
                "reason": "binding_mismatch",
            }
        ]
        with self.assertRaises(SemanticValidationError):
            decode_observatory_snapshot(cross_scope_conflict)

        conflict = semantic_snapshot()
        duplicate_route_claim = deepcopy(conflict["claims"][4])
        duplicate_route_claim["id"] = "claim-challenge-disagreeing"
        duplicate_route_claim["value"] = "rejected"
        conflict["claims"].append(duplicate_route_claim)
        conflict["conflicts"] = [
            {
                "claim_ids": ["claim-challenge", "claim-challenge-disagreeing"],
                "scope": {"kind": "route", "id": "route-primary"},
                "reason": "value_mismatch",
            }
        ]
        decoded = decode_observatory_snapshot(conflict)
        qualification = qualify_observatory_snapshot(decoded, now=NOW)
        self.assertFalse(qualification.live)
        self.assertIn("conflicts_present", qualification.reasons)

        unreported = semantic_snapshot()
        unreported["claims"].append(duplicate_route_claim)
        with self.assertRaises(SemanticValidationError):
            decode_observatory_snapshot(unreported)

    def test_live_gate_requires_current_successful_challenge_and_completed_real_lifecycle(self):
        qualified = qualify_observatory_snapshot(decode_observatory_snapshot(semantic_snapshot()), now=NOW)
        self.assertTrue(qualified.live)
        self.assertEqual(qualified.reasons, ())

        cases: list[tuple[str, callable]] = [
            ("failed challenge", lambda value: value["route_challenge"].update(status="failed")),
            ("non-completed request", lambda value: value["request_lifecycle"].update(state="failed")),
            (
                "stale",
                lambda value: value.update(
                    freshness=freshness(
                        "2026-07-18T11:00:00Z",
                        "2026-07-18T11:30:00Z",
                    )
                ),
            ),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                candidate = semantic_snapshot()
                mutate(candidate)
                if name == "stale":
                    for claim in candidate["claims"]:
                        claim["freshness"] = deepcopy(candidate["freshness"])
                    candidate["route_challenge"]["freshness"] = deepcopy(candidate["freshness"])
                    candidate["request_lifecycle"]["freshness"] = deepcopy(candidate["freshness"])
                decoded = decode_observatory_snapshot(candidate)
                self.assertFalse(qualify_observatory_snapshot(decoded, now=NOW).live)

        wrong_provenance = semantic_snapshot()
        wrong_provenance["request_lifecycle"]["provenance"] = provenance(
            "gateway_projection", "mycelium_gateway"
        )
        with self.assertRaises(SemanticValidationError):
            decode_observatory_snapshot(wrong_provenance)


class SemanticGatewayTests(unittest.TestCase):
    @staticmethod
    async def request(app: ObservatoryASGIApplication, path: str) -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []
        received = False

        async def receive() -> dict[str, object]:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
        }
        await app(scope, receive, send)
        return sent

    @staticmethod
    def body(messages: list[dict[str, object]]) -> bytes:
        return b"".join(
            message.get("body", b"")
            for message in messages
            if message.get("type") == "http.response.body"
        )

    def test_one_owner_publishes_event_v1_to_snapshot_and_sse(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "observatory.json"
            runtime = build_semantic_observatory_gateway(
                state_path=state_path,
                read_policy=lambda _scope: True,
                now=lambda: NOW,
                heartbeat_interval=0.01,
                poll_interval=0.002,
            )
            try:
                publication = runtime.owner.publish_snapshot(semantic_snapshot())
                self.assertEqual(publication.generation, 1)
                self.assertEqual(publication.protocol, OBSERVATORY_EVENT_PROTOCOL)

                snapshot_messages = asyncio.run(
                    self.request(runtime.app, "/v1/observatory/snapshot")
                )
                snapshot_document = json.loads(self.body(snapshot_messages))
                self.assertEqual(snapshot_document, semantic_event(1))

                event_messages = asyncio.run(self.request(runtime.app, "/v1/observatory/events"))
                event_body = self.body(event_messages)
                self.assertIn(b"id: 1\nevent: snapshot\ndata: ", event_body)
                event_document = json.loads(event_body.split(b"data: ", 1)[1].split(b"\n\n", 1)[0])
                self.assertEqual(event_document, semantic_event(1))
            finally:
                runtime.close()

    def test_second_publication_sse_owner_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "observatory.json"
            first = build_semantic_observatory_gateway(
                state_path=state_path,
                read_policy=lambda _scope: True,
                now=lambda: NOW,
            )
            try:
                with self.assertRaises(ObservatoryOwnerError):
                    build_semantic_observatory_gateway(
                        state_path=state_path,
                        read_policy=lambda _scope: True,
                        now=lambda: NOW,
                    )
            finally:
                first.close()

    def test_privacy_canary_rejection_never_advances_or_reaches_disk_get_or_sse(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "observatory.json"
            runtime = build_semantic_observatory_gateway(
                state_path=state_path,
                read_policy=lambda _scope: True,
                now=lambda: NOW,
                heartbeat_interval=0.01,
                poll_interval=0.002,
            )
            try:
                bad = semantic_snapshot()
                bad["debug_note"] = CANARY
                with self.assertRaises(SemanticValidationError):
                    runtime.owner.publish_snapshot(bad)
                self.assertFalse(state_path.exists())

                accepted = runtime.owner.publish_snapshot(semantic_snapshot())
                self.assertEqual(accepted.generation, 1)
                snapshot_messages = asyncio.run(
                    self.request(runtime.app, "/v1/observatory/snapshot")
                )
                event_messages = asyncio.run(self.request(runtime.app, "/v1/observatory/events"))
                surfaces = state_path.read_bytes() + self.body(snapshot_messages) + self.body(event_messages)
                self.assertNotIn(CANARY.encode("ascii"), surfaces)
            finally:
                runtime.close()

    def test_stale_snapshot_is_not_published_or_generation_advanced(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "observatory.json"
            runtime = build_semantic_observatory_gateway(
                state_path=state_path,
                read_policy=lambda _scope: True,
                now=lambda: NOW,
            )
            try:
                stale = semantic_snapshot()
                stale["freshness"] = freshness(
                    "2026-07-18T11:00:00Z",
                    "2026-07-18T11:30:00Z",
                )
                for claim in stale["claims"]:
                    claim["freshness"] = deepcopy(stale["freshness"])
                stale["route_challenge"]["freshness"] = deepcopy(stale["freshness"])
                stale["request_lifecycle"]["freshness"] = deepcopy(stale["freshness"])
                with self.assertRaises(StaleSemanticSnapshotError):
                    runtime.owner.publish_snapshot(stale)
                self.assertFalse(state_path.exists())
                self.assertEqual(runtime.owner.publish_snapshot(semantic_snapshot()).generation, 1)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
