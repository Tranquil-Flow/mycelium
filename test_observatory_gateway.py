from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
from pathlib import Path
import tempfile
from typing import Any, Callable
import unittest
from unittest import mock

from mycelium_gateway.asgi import ObservatoryASGIApplication
from mycelium_gateway.init import build_observatory_gateway
from mycelium_gateway.observatory import (
    BundleValidationError,
    CoherentSnapshotPublisher,
    GenerationExhaustedError,
    PublisherStateError,
    SubscriberLimitError,
)


PROTOCOL = "mycelium.observatory_stream.v1"


def observatory_bundle(marker: str = "initial") -> dict[str, object]:
    return {
        "snapshot": {
            "source": {"scenario": marker},
            "nodes": [{"id": "node-b"}, {"id": "node-a"}],
        },
        "incidents": [],
        "provisioning": {"state": "verified", "route_ready": False},
    }


def _publish_in_process(
    state_path: str,
    marker: str,
    start: Any,
    results: Any,
) -> None:
    try:
        publisher = CoherentSnapshotPublisher(state_path)
        start.wait(5)
        publication = publisher.publish(observatory_bundle(marker))
        results.put((publication.generation, None))
    except BaseException as exc:  # pragma: no cover - relayed to parent process
        cause = f"; cause={exc.__cause__!r}" if exc.__cause__ is not None else ""
        results.put((None, f"{type(exc).__name__}: {exc}{cause}"))


class ASGIHarness:
    @staticmethod
    def scope(
        path: str,
        *,
        method: str = "GET",
        headers: list[tuple[bytes, bytes]] | None = None,
        scope_type: str = "http",
    ) -> dict[str, object]:
        if scope_type == "websocket":
            return {
                "type": "websocket",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "headers": headers or [],
                "scheme": "ws",
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 80),
                "subprotocols": [],
            }
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
        }

    @staticmethod
    async def request(
        app: ObservatoryASGIApplication,
        path: str,
        *,
        method: str = "GET",
        headers: list[tuple[bytes, bytes]] | None = None,
        scope_type: str = "http",
    ) -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []
        received = False

        async def receive() -> dict[str, object]:
            nonlocal received
            if scope_type == "websocket":
                if not received:
                    received = True
                    return {"type": "websocket.connect"}
                return {"type": "websocket.disconnect", "code": 1000}
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await app(ASGIHarness.scope(path, method=method, headers=headers, scope_type=scope_type), receive, send)
        return sent

    @staticmethod
    async def stream(
        app: ObservatoryASGIApplication,
        *,
        expected_snapshots: int = 0,
        disconnect_on_heartbeat: bool = False,
        headers: list[tuple[bytes, bytes]] | None = None,
        publish_after_start: callable | None = None,
    ) -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []
        incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b"", "more_body": False})
        snapshots = 0

        async def receive() -> dict[str, object]:
            return await incoming.get()

        async def send(message: dict[str, object]) -> None:
            nonlocal snapshots
            sent.append(message)
            if message.get("type") != "http.response.body":
                return
            body = message.get("body", b"")
            if isinstance(body, bytes) and b"event: snapshot\n" in body:
                snapshots += 1
                if snapshots >= expected_snapshots > 0:
                    await incoming.put({"type": "http.disconnect"})
            if disconnect_on_heartbeat and body == b": heartbeat\n\n":
                await incoming.put({"type": "http.disconnect"})

        task = asyncio.create_task(
            app(
                ASGIHarness.scope("/v1/observatory/events", headers=headers),
                receive,
                send,
            )
        )
        if publish_after_start is not None:
            while not any(message.get("type") == "http.response.start" for message in sent):
                await asyncio.sleep(0)
            publish_after_start()
        await asyncio.wait_for(task, timeout=2)
        return sent


def response_headers(messages: list[dict[str, object]]) -> dict[bytes, bytes]:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return dict(start["headers"])


def response_body(messages: list[dict[str, object]]) -> bytes:
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message.get("type") == "http.response.body"
    )


def snapshot_frames(messages: list[dict[str, object]]) -> list[bytes]:
    return [
        message.get("body", b"")
        for message in messages
        if message.get("type") == "http.response.body"
        and b"event: snapshot\n" in message.get("body", b"")
    ]


class CoherentSnapshotPublisherTests(unittest.TestCase):
    def test_atomic_publication_is_immutable_and_snapshot_json_is_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "publisher" / "state.json"
            publisher = CoherentSnapshotPublisher(state_path)
            source = observatory_bundle()

            publication = publisher.publish(source)
            source["snapshot"]["source"]["scenario"] = "mutated-after-publication"

            expected = {
                "protocol": PROTOCOL,
                "generation": 1,
                "bundle": observatory_bundle(),
            }
            expected_bytes = json.dumps(
                expected,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            self.assertEqual(publication.generation, 1)
            self.assertEqual(publication.envelope_json, expected_bytes)
            self.assertEqual(publisher.snapshot_json(), expected_bytes)
            self.assertEqual(state_path.read_bytes(), expected_bytes)
            self.assertEqual(publication.bundle["snapshot"]["source"]["scenario"], "initial")
            with self.assertRaises(TypeError):
                publication.bundle["snapshot"]["source"]["scenario"] = "forbidden"

    def test_restart_recovers_latest_snapshot_and_continues_monotonically(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            first = CoherentSnapshotPublisher(state_path)
            self.assertEqual(first.publish(observatory_bundle("one")).generation, 1)
            self.assertEqual(first.publish(observatory_bundle("two")).generation, 2)

            restarted = CoherentSnapshotPublisher(state_path)
            self.assertEqual(restarted.current_publication().generation, 2)
            self.assertEqual(restarted.current_envelope()["bundle"], observatory_bundle("two"))
            self.assertEqual(restarted.publish(observatory_bundle("three")).generation, 3)

    def test_two_process_publishers_never_allocate_duplicate_generations(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(target=_publish_in_process, args=(state_path, marker, start, results))
                for marker in ("left", "right")
            ]
            for process in processes:
                process.start()
            start.set()
            outcomes = [results.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

            self.assertEqual([error for _, error in outcomes], [None, None])
            self.assertEqual(sorted(generation for generation, _ in outcomes), [1, 2])
            restarted = CoherentSnapshotPublisher(state_path)
            self.assertEqual(restarted.current_publication().generation, 2)

    def test_snapshot_to_subscribe_handoff_replays_every_update_without_gap(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json", replay_capacity=4)
            first = publisher.publish(observatory_bundle("one"))
            captured_snapshot = publisher.current_envelope()
            self.assertEqual(captured_snapshot["generation"], first.generation)
            publisher.publish(observatory_bundle("two"))

            subscription = publisher.subscribe(last_event_id=first.generation)
            self.assertEqual([item.generation for item in subscription.replay], [2])
            publisher.publish(observatory_bundle("three"))
            self.assertEqual(subscription.get_nowait().generation, 3)
            subscription.close()

    def test_replay_gap_check_never_allocates_generation_sized_ranges(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            envelope = {
                "protocol": PROTOCOL,
                "generation": 2**53 - 1,
                "bundle": observatory_bundle("latest"),
            }
            state_path.write_text(
                json.dumps(envelope, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            publisher = CoherentSnapshotPublisher(state_path)

            with mock.patch(
                "mycelium_gateway.observatory.range",
                side_effect=AssertionError("replay gap check allocated a generation-sized range"),
                create=True,
            ):
                subscription = publisher.subscribe(last_event_id=0)

            self.assertEqual([item.generation for item in subscription.replay], [2**53 - 1])
            subscription.close()

    def test_slow_subscriber_is_bounded_and_disconnected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(
                Path(directory) / "state.json",
                subscriber_queue_size=1,
            )
            publisher.publish(observatory_bundle("one"))
            subscription = publisher.subscribe(last_event_id=1)
            publisher.publish(observatory_bundle("two"))
            publisher.publish(observatory_bundle("three"))

            self.assertTrue(subscription.closed)
            self.assertEqual(subscription.disconnect_reason, "slow_consumer")
            self.assertLessEqual(subscription.queued_count, 1)
            self.assertEqual(publisher.subscriber_count, 0)

    def test_subscriber_limit_and_cleanup_are_enforced(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(
                Path(directory) / "state.json",
                max_subscribers=1,
            )
            publisher.publish(observatory_bundle())
            first = publisher.subscribe(last_event_id=1)
            self.assertEqual(publisher.subscriber_count, 1)
            with self.assertRaises(SubscriberLimitError):
                publisher.subscribe(last_event_id=1)
            first.close()
            self.assertEqual(publisher.subscriber_count, 0)
            second = publisher.subscribe(last_event_id=1)
            second.close()

    def test_rejects_malformed_oversized_and_sensitive_bundles_without_advancing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            publisher = CoherentSnapshotPublisher(
                state_path,
                max_payload_bytes=700,
                max_nesting=8,
            )
            malformed = [
                None,
                {},
                {"snapshot": {}, "incidents": [], "provisioning": {}, "extra": object()},
                {"snapshot": {"bad": math.nan}, "incidents": [], "provisioning": {}},
                {"snapshot": {"bad": math.inf}, "incidents": [], "provisioning": {}},
                {"snapshot": {"bad": {1, 2}}, "incidents": [], "provisioning": {}},
                {"snapshot": {"raw_tensor": [1.0]}, "incidents": [], "provisioning": {}},
                {"snapshot": {"prompt": "private request"}, "incidents": [], "provisioning": {}},
                {"snapshot": {"рrompt": "homoglyph bypass"}, "incidents": [], "provisioning": {}},
                {"snapshot": {"token_ids": [1, 2]}, "incidents": [], "provisioning": {}},
                {"snapshot": {"model_weights": [1, 2]}, "incidents": [], "provisioning": {}},
                {"snapshot": {"private_key": "not-for-evidence"}, "incidents": [], "provisioning": {}},
                {"snapshot": {"api_secret": "not-for-evidence"}, "incidents": [], "provisioning": {}},
                {
                    "snapshot": {"note": "-----BEGIN PRIVATE KEY-----\nredacted"},
                    "incidents": [],
                    "provisioning": {},
                },
                {
                    "snapshot": {"note": "-----BEGIN VENDOR PRIVATE KEY-----\nredacted"},
                    "incidents": [],
                    "provisioning": {},
                },
                {
                    "snapshot": {"note": "gh" + "p_" + ("a" * 36)},
                    "incidents": [],
                    "provisioning": {},
                },
                {
                    "snapshot": {"note": "AK" + "IA" + ("A" * 16)},
                    "incidents": [],
                    "provisioning": {},
                },
                observatory_bundle("x" * 2_000),
            ]
            nested: dict[str, object] = {}
            cursor = nested
            for _ in range(10):
                child: dict[str, object] = {}
                cursor["child"] = child
                cursor = child
            malformed.append({"snapshot": nested, "incidents": [], "provisioning": {}})

            for bundle in malformed:
                with self.subTest(bundle=repr(bundle)[:80]):
                    with self.assertRaises(BundleValidationError):
                        publisher.publish(bundle)
                    self.assertIsNone(publisher.current_publication())
                    self.assertFalse(state_path.exists())

            accepted = publisher.publish(observatory_bundle("safe"))
            self.assertEqual(accepted.generation, 1)

    def test_deterministic_json_is_independent_of_mapping_insertion_order(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            first = CoherentSnapshotPublisher(Path(directory) / "first.json")
            second = CoherentSnapshotPublisher(Path(directory) / "second.json")
            bundle_a = observatory_bundle()
            bundle_b = {
                "provisioning": {"route_ready": False, "state": "verified"},
                "incidents": [],
                "snapshot": {
                    "nodes": [{"id": "node-b"}, {"id": "node-a"}],
                    "source": {"scenario": "initial"},
                },
            }
            self.assertEqual(first.publish(bundle_a).envelope_json, second.publish(bundle_b).envelope_json)

    def test_corrupt_persisted_state_fails_closed_without_leaking_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(PublisherStateError) as raised:
                CoherentSnapshotPublisher(state_path)
            self.assertNotIn(str(state_path), str(raised.exception))

    def test_safe_generation_limit_cannot_wrap(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            envelope = {
                "protocol": PROTOCOL,
                "generation": 2**53 - 1,
                "bundle": observatory_bundle(),
            }
            state_path.write_text(
                json.dumps(envelope, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            publisher = CoherentSnapshotPublisher(state_path)
            with self.assertRaises(GenerationExhaustedError):
                publisher.publish(observatory_bundle("next"))
            self.assertEqual(publisher.current_publication().generation, 2**53 - 1)


class ObservatoryASGITests(unittest.TestCase):
    def authorized_app(
        self,
        publisher: CoherentSnapshotPublisher,
        *,
        heartbeat_interval: float = 0.02,
    ) -> ObservatoryASGIApplication:
        return ObservatoryASGIApplication(
            publisher,
            read_policy=lambda scope: scope.get("client") == ("127.0.0.1", 12345),
            heartbeat_interval=heartbeat_interval,
            poll_interval=0.002,
        )

    def test_default_authorization_denies_snapshot_and_events(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json")
            publisher.publish(observatory_bundle())
            app = ObservatoryASGIApplication(publisher)
            for path in ("/v1/observatory/snapshot", "/v1/observatory/events"):
                messages = asyncio.run(ASGIHarness.request(app, path))
                self.assertEqual(messages[0]["status"], 403)
                self.assertEqual(json.loads(response_body(messages)), {"error": "forbidden"})

    def test_injected_policy_authorizes_exact_snapshot_with_no_cache_or_cors(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json")
            publication = publisher.publish(observatory_bundle())
            messages = asyncio.run(
                ASGIHarness.request(self.authorized_app(publisher), "/v1/observatory/snapshot")
            )
            headers = response_headers(messages)

            self.assertEqual(messages[0]["status"], 200)
            self.assertEqual(response_body(messages), publication.envelope_json)
            self.assertEqual(headers[b"content-type"], b"application/json")
            self.assertEqual(headers[b"cache-control"], b"no-store")
            self.assertNotIn(b"access-control-allow-origin", headers)
            self.assertNotIn(b"access-control-allow-credentials", headers)

    def test_async_read_policy_is_supported_and_policy_errors_are_generic(self):
        import tempfile

        async def allow(scope: dict[str, object]) -> bool:
            return scope["path"] == "/v1/observatory/snapshot"

        def explode(scope: dict[str, object]) -> bool:
            raise RuntimeError("internal path /private/operator/state and secret detail")

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json")
            publisher.publish(observatory_bundle())
            allowed = asyncio.run(
                ASGIHarness.request(
                    ObservatoryASGIApplication(publisher, read_policy=allow),
                    "/v1/observatory/snapshot",
                )
            )
            denied = asyncio.run(
                ASGIHarness.request(
                    ObservatoryASGIApplication(publisher, read_policy=explode),
                    "/v1/observatory/snapshot",
                )
            )
            self.assertEqual(allowed[0]["status"], 200)
            self.assertEqual(denied[0]["status"], 403)
            self.assertEqual(json.loads(response_body(denied)), {"error": "forbidden"})
            self.assertNotIn(b"/private", response_body(denied))
            self.assertNotIn(b"secret detail", response_body(denied))

    def test_last_event_id_replays_all_retained_complete_envelopes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json", replay_capacity=4)
            for marker in ("one", "two", "three"):
                publisher.publish(observatory_bundle(marker))
            messages = asyncio.run(
                ASGIHarness.stream(
                    self.authorized_app(publisher),
                    expected_snapshots=2,
                    headers=[(b"last-event-id", b"1")],
                )
            )
            frames = snapshot_frames(messages)
            self.assertEqual(len(frames), 2)
            self.assertTrue(frames[0].startswith(b"id: 2\nevent: snapshot\ndata: "))
            self.assertTrue(frames[1].startswith(b"id: 3\nevent: snapshot\ndata: "))
            envelopes = [json.loads(frame.split(b"data: ", 1)[1]) for frame in frames]
            self.assertEqual([item["generation"] for item in envelopes], [2, 3])
            self.assertEqual(envelopes[0]["bundle"], observatory_bundle("two"))
            self.assertEqual(envelopes[1]["bundle"], observatory_bundle("three"))
            self.assertEqual(publisher.subscriber_count, 0)

    def test_replay_gap_emits_only_latest_full_snapshot(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json", replay_capacity=2)
            for marker in ("one", "two", "three", "four"):
                publisher.publish(observatory_bundle(marker))
            messages = asyncio.run(
                ASGIHarness.stream(
                    self.authorized_app(publisher),
                    expected_snapshots=1,
                    headers=[(b"last-event-id", b"1")],
                )
            )
            frames = snapshot_frames(messages)
            self.assertEqual(len(frames), 1)
            self.assertTrue(frames[0].startswith(b"id: 4\nevent: snapshot\ndata: "))
            envelope = json.loads(frames[0].split(b"data: ", 1)[1])
            self.assertEqual(envelope["bundle"], observatory_bundle("four"))

    def test_future_cursor_is_reset_with_latest_full_snapshot(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json", replay_capacity=2)
            publisher.publish(observatory_bundle("current"))
            messages = asyncio.run(
                ASGIHarness.stream(
                    self.authorized_app(publisher),
                    expected_snapshots=1,
                    headers=[(b"last-event-id", b"99")],
                )
            )
            frames = snapshot_frames(messages)
            self.assertEqual(len(frames), 1)
            self.assertTrue(frames[0].startswith(b"id: 1\nevent: snapshot\ndata: "))
            envelope = json.loads(frames[0].split(b"data: ", 1)[1])
            self.assertEqual(envelope["bundle"], observatory_bundle("current"))

    def test_sse_handoff_captures_publication_after_stream_start(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json")
            publisher.publish(observatory_bundle("one"))
            messages = asyncio.run(
                ASGIHarness.stream(
                    self.authorized_app(publisher),
                    expected_snapshots=1,
                    headers=[(b"last-event-id", b"1")],
                    publish_after_start=lambda: publisher.publish(observatory_bundle("two")),
                )
            )
            frames = snapshot_frames(messages)
            self.assertEqual(len(frames), 1)
            self.assertTrue(frames[0].startswith(b"id: 2\nevent: snapshot\ndata: "))
            self.assertEqual(publisher.subscriber_count, 0)

    def test_sse_sends_fixed_bounded_heartbeat_and_cleans_up(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json")
            publisher.publish(observatory_bundle())
            messages = asyncio.run(
                ASGIHarness.stream(
                    self.authorized_app(publisher, heartbeat_interval=0.01),
                    disconnect_on_heartbeat=True,
                    headers=[(b"last-event-id", b"1")],
                )
            )
            heartbeats = [
                message["body"]
                for message in messages
                if message.get("type") == "http.response.body"
                and message.get("body") == b": heartbeat\n\n"
            ]
            self.assertEqual(heartbeats, [b": heartbeat\n\n"])
            self.assertLessEqual(len(heartbeats[0]), 32)
            self.assertEqual(publisher.subscriber_count, 0)
            headers = response_headers(messages)
            self.assertEqual(headers[b"content-type"], b"text/event-stream; charset=utf-8")
            self.assertEqual(headers[b"cache-control"], b"no-store")
            self.assertNotIn(b"access-control-allow-origin", headers)

    def test_stream_failure_after_headers_never_sends_a_second_response(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json")
            publisher.publish(observatory_bundle())
            app = self.authorized_app(publisher)

            async def exercise() -> list[dict[str, Any]]:
                sent: list[dict[str, Any]] = []
                incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
                await incoming.put({"type": "http.request", "body": b"", "more_body": False})

                async def receive() -> dict[str, Any]:
                    return await incoming.get()

                async def send(message: dict[str, Any]) -> None:
                    sent.append(message)
                    if message.get("type") == "http.response.body":
                        raise RuntimeError("simulated stream transport failure")

                await app(ASGIHarness.scope("/v1/observatory/events"), receive, send)
                return sent

            messages = asyncio.run(exercise())
            starts = [message for message in messages if message.get("type") == "http.response.start"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["status"], 200)
            self.assertEqual(publisher.subscriber_count, 0)

    def test_snapshot_transport_failure_after_headers_never_sends_a_second_response(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json")
            publisher.publish(observatory_bundle())
            app = self.authorized_app(publisher)

            async def exercise() -> list[dict[str, Any]]:
                sent: list[dict[str, Any]] = []

                async def receive() -> dict[str, Any]:
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message: dict[str, Any]) -> None:
                    sent.append(message)
                    if message.get("type") == "http.response.body":
                        raise RuntimeError("simulated snapshot transport failure")

                try:
                    await app(ASGIHarness.scope("/v1/observatory/snapshot"), receive, send)
                except RuntimeError:
                    pass
                return sent

            messages = asyncio.run(exercise())
            starts = [message for message in messages if message.get("type") == "http.response.start"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["status"], 200)

    def test_mutating_methods_unknown_paths_invalid_cursor_and_upgrades_are_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "state.json")
            publisher.publish(observatory_bundle())
            app = self.authorized_app(publisher)
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                for path in ("/v1/observatory/snapshot", "/v1/observatory/events"):
                    with self.subTest(method=method, path=path):
                        messages = asyncio.run(ASGIHarness.request(app, path, method=method))
                        self.assertEqual(messages[0]["status"], 405)
                        self.assertEqual(response_headers(messages)[b"allow"], b"GET")
            unknown = asyncio.run(ASGIHarness.request(app, "/v1/private/internal"))
            self.assertEqual(unknown[0]["status"], 404)
            self.assertNotIn(b"/v1/private/internal", response_body(unknown))
            invalid_cursor = asyncio.run(
                ASGIHarness.request(
                    app,
                    "/v1/observatory/events",
                    headers=[(b"last-event-id", b"-1")],
                )
            )
            self.assertEqual(invalid_cursor[0]["status"], 400)
            upgraded = asyncio.run(
                ASGIHarness.request(
                    app,
                    "/v1/observatory/events",
                    scope_type="websocket",
                )
            )
            self.assertEqual(upgraded, [{"type": "websocket.close", "code": 1008}])
            http_upgrade = asyncio.run(
                ASGIHarness.request(
                    app,
                    "/v1/observatory/events",
                    headers=[(b"upgrade", b"websocket")],
                )
            )
            self.assertEqual(http_upgrade[0]["status"], 400)

    def test_empty_publisher_is_unavailable_without_internal_detail(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            publisher = CoherentSnapshotPublisher(Path(directory) / "private" / "state.json")
            app = self.authorized_app(publisher)
            messages = asyncio.run(ASGIHarness.request(app, "/v1/observatory/snapshot"))
            self.assertEqual(messages[0]["status"], 503)
            self.assertEqual(json.loads(response_body(messages)), {"error": "snapshot_unavailable"})
            self.assertNotIn(str(directory).encode(), response_body(messages))

    def test_builder_keeps_default_policy_deny_only_and_has_no_control_surface(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            runtime = build_observatory_gateway(state_path=Path(directory) / "state.json")
            runtime.publisher.publish(observatory_bundle())
            messages = asyncio.run(
                ASGIHarness.request(runtime.app, "/v1/observatory/snapshot")
            )
            self.assertEqual(messages[0]["status"], 403)
            public_names = set(dir(runtime.app)) | set(dir(runtime.publisher))
            forbidden = {"infer", "generate", "route", "submit", "mutate", "publish_prompt"}
            self.assertTrue(forbidden.isdisjoint(public_names))


if __name__ == "__main__":
    unittest.main()
