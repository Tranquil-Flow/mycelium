from __future__ import annotations

import asyncio
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from mycelium_live.route import FakeLiveRoute, RouteIdentity
from mycelium_live.supervisor import (
    LiveObservatoryApplication,
    _m18_replica_plan,
    _placement_projection,
    _workload_comparison,
    _qualify_open_route,
    _validate_route_identity,
    build_live_stack,
    create_server,
    run_physical_server,
)
from mycelium_request_gateway.asgi import MAX_REQUEST_BODY_BYTES
from mycelium_ui_gateway.validation import validate_observatory_envelope
from physical_inference_qualification import ControllerError


def test_build_live_stack_returns_app_and_health_source(
    live_graph, deployment_dir
) -> None:
    route = FakeLiveRoute(scripted_tokens=(4599,))
    route.open()
    stack = build_live_stack(
        route=route,
        deployment_dir=deployment_dir,
        execution_graph=live_graph,
        bearer_token="test-token",
    )
    assert stack.app is not None
    assert stack.health.current() is None
    swarm = stack.app._coordinator.status()
    assert [node["member_id"] for node in swarm["native_nodes"]] == [
        "node-a",
        "node-b",
    ]
    assert all(node["membership_state"] == "assigned" for node in swarm["native_nodes"])


def test_health_publishes_after_challenge(deployment_dir, qualified_route) -> None:
    qualification, graph = qualified_route
    route = FakeLiveRoute(scripted_tokens=(4599,))
    route.open()
    stack = build_live_stack(
        route=route,
        deployment_dir=deployment_dir,
        execution_graph=graph,
        bearer_token="test-token",
    )
    stack.health.publish(qualification)
    assert stack.health.current() is qualification
    assert all(
        node["membership_state"] == "qualified"
        for node in stack.app._coordinator.status()["native_nodes"]
    )


def test_observatory_projection_is_browser_safe(
    deployment_dir, qualified_route
) -> None:
    qualification, graph = qualified_route
    route = FakeLiveRoute(scripted_tokens=(4599,))
    route.open()
    stack = build_live_stack(
        route=route,
        deployment_dir=deployment_dir,
        execution_graph=graph,
        bearer_token="test-token",
    )
    stack.health.publish(qualification)
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    observatory = stack.app._observatory_app
    asyncio.run(
        observatory(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/observatory/snapshot",
            },
            receive,
            send,
        )
    )
    document = json.loads(
        b"".join(
            message.get("body", b"")
            for message in sent
            if message.get("type") == "http.response.body"
        )
    )
    validate_observatory_envelope(document)
    assert document["bundle"]["provisioning"]["route_ready"] is True


def test_observatory_generation_is_restart_monotonic_by_wall_clock(monkeypatch) -> None:
    health = SimpleNamespace(current=lambda: None)
    monkeypatch.setattr("mycelium_live.supervisor.time.time", lambda: 2_000_000.0)

    first = LiveObservatoryApplication(health)._envelope()
    monkeypatch.setattr("mycelium_live.supervisor.time.time", lambda: 2_000_001.0)
    restarted = LiveObservatoryApplication(health)._envelope()

    assert restarted["generation"] > first["generation"]


def test_qualification_renewal_reruns_challenge_and_releases_private_tokens(
    monkeypatch,
) -> None:
    qualification = object()

    class RenewableRoute:
        startup_challenge = ((1, 2), (3, 4))

        def __init__(self) -> None:
            self.released = []
            self.request_id = None

        def infer(self, token_ids, *, max_new_tokens, request_id, sink):
            assert token_ids == (1, 2)
            assert max_new_tokens == 2
            self.request_id = request_id
            return SimpleNamespace(token_ids=(3, 4))

        def counters(self):
            return SimpleNamespace(fatal=None)

        def live_attestation(self, *, request_id):
            assert request_id == self.request_id
            return {"request_id": request_id}

        def release_request(self, request_id):
            self.released.append(request_id)

    route = RenewableRoute()
    monkeypatch.setattr(
        "mycelium_live.supervisor.issue_live_route_qualification",
        lambda attestation, **expected: (
            qualification
            if attestation["request_id"] == route.request_id
            and expected["expected_prompt_token_ids"] == (1, 2)
            and expected["expected_output_token_ids"] == (3, 4)
            else None
        ),
    )

    assert _qualify_open_route(route) is qualification
    assert route.released == [route.request_id]


def _identity(endpoint_ids: tuple[str, ...]) -> RouteIdentity:
    return RouteIdentity(
        deployment_id="deployment",
        model_id="model",
        resolved_commit="commit",
        endpoint_ids=endpoint_ids,
    )


def _graph(node_ids: tuple[str, ...]):
    return SimpleNamespace(
        stages=tuple(
            SimpleNamespace(
                placements=(SimpleNamespace(node_id=node_id),),
            )
            for node_id in node_ids
        )
    )


def test_physical_server_surfaces_safe_startup_remote_code(monkeypatch) -> None:
    class RejectingRoute:
        execution_graph = _graph(("node-a", "node-b", "node-c"))
        startup_challenge = ((1,), (2,))

        def __init__(self) -> None:
            self.closed = False
            self.cleaned = False

        def open(self):
            return _identity(("endpoint-a", "endpoint-b", "endpoint-c"))

        def infer(self, *_args, **_kwargs):
            raise ControllerError(
                "node_command_rejected",
                remote_code="source_binding_mismatch",
            )

        def release_request(self, _request_id) -> None:
            return

        def close(self) -> None:
            self.closed = True

        def cleanup(self) -> None:
            self.cleaned = True

    route = RejectingRoute()
    monkeypatch.setattr(
        "mycelium_live.supervisor.PhysicalLiveRoute.from_operator_plan",
        lambda _plan, **_kwargs: route,
    )

    with pytest.raises(
        RuntimeError,
        match="startup_route_rejected:source_binding_mismatch",
    ):
        run_physical_server(
            operator_plan=SimpleNamespace(),
            host="127.0.0.1",
            port=8788,
            seed_state_root=Path("/private/test-seed"),
        )

    assert route.closed is True
    assert route.cleaned is True


def test_route_identity_accepts_one_distinct_endpoint_per_three_host_graph() -> None:
    _validate_route_identity(
        _identity(("endpoint-0", "endpoint-1", "endpoint-2")),
        _graph(("node-0", "node-1", "node-2")),
    )


@pytest.mark.parametrize(
    "endpoint_ids",
    [
        ("endpoint-0", "endpoint-1"),
        ("endpoint-0", "endpoint-1", "endpoint-1"),
        ("endpoint-0", "endpoint-1", "endpoint-2", "endpoint-3"),
    ],
)
def test_route_identity_rejects_missing_duplicate_or_extra_endpoints(
    endpoint_ids: tuple[str, ...],
) -> None:
    with pytest.raises(RuntimeError, match="^startup_endpoint_identity_invalid$"):
        _validate_route_identity(
            _identity(endpoint_ids),
            _graph(("node-0", "node-1", "node-2")),
        )


def test_loopback_wrapper_rejects_oversized_body_before_asgi(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()

    async def app(*_args):
        raise AssertionError("oversized_body_reached_asgi")

    server = create_server(
        app=app,
        route=route,
        static_root=static_root,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/v1/inference",
            body=b"",
            headers={"content-length": str(MAX_REQUEST_BODY_BYTES + 1)},
        )
        response = connection.getresponse()
        assert response.status == 413
        assert json.loads(response.read()) == {"error": "request_body_too_large"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_candidate_promotion_and_rollback_posts_are_same_origin_and_bounded(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    calls = []

    route = SimpleNamespace(
        canary_candidate=lambda candidate_id, **values: (
            calls.append(("canary", candidate_id, values))
            or {"candidate_deployment_id": candidate_id, **values}
        ),
        promote_candidate=lambda report: (
            calls.append(("promote", report))
            or {"selected_deployment_id": report["candidate_deployment_id"]}
        ),
        rollback_candidate=lambda candidate_id, *, reason: (
            calls.append(("rollback", candidate_id, reason))
            or {"selected_deployment_id": "incumbent"}
        ),
    )

    async def app(*_args):
        raise AssertionError("candidate_endpoint_reached_asgi")

    server = create_server(
        app=app,
        route=route,
        static_root=static_root,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        canary = {
            "candidate_deployment_id": "candidate",
            "case_id": "case-a",
            "prompt": "Arbitrary prompt",
            "max_new_tokens": 8,
        }
        connection.request(
            "POST",
            "/__mycelium/candidates/canary",
            body=json.dumps(canary),
            headers={"content-type": "application/json", "origin": origin},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == canary

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        report = {"candidate_deployment_id": "candidate"}
        connection.request(
            "POST",
            "/__mycelium/candidates/promote",
            body=json.dumps(report),
            headers={"content-type": "application/json", "origin": origin},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"selected_deployment_id": "candidate"}

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/candidates/rollback",
            body=json.dumps(
                {
                    "candidate_deployment_id": "candidate",
                    "reason": "canary_regression",
                }
            ),
            headers={"content-type": "application/json", "origin": origin},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"selected_deployment_id": "incumbent"}

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/candidates/promote",
            body=json.dumps(report),
            headers={
                "content-type": "application/json",
                "origin": "https://attacker.invalid",
            },
        )
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read()) == {"error": "origin_mismatch"}
        assert calls == [
            (
                "canary",
                "candidate",
                {
                    "case_id": "case-a",
                    "prompt": "Arbitrary prompt",
                    "max_new_tokens": 8,
                },
            ),
            ("promote", report),
            ("rollback", "candidate", "canary_regression"),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_m17_swarm_evidence_endpoint_is_read_only_and_fail_closed(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    expected = {
        "protocol": "mycelium.live_swarm_resource_observations.v1",
        "signed_snapshots": [],
        "route_ready": False,
    }
    calls = []
    route = SimpleNamespace(
        m17_swarm_evidence=lambda: calls.append("snapshot") or expected,
    )

    async def app(*_args):
        raise AssertionError("m17_swarm_evidence_reached_asgi")

    server = create_server(
        app=app,
        route=route,
        static_root=static_root,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/__mycelium/m17-swarm-evidence")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == expected
        assert calls == ["snapshot"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_m18_plan_and_runtime_endpoints_are_read_only(tmp_path: Path) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    plan = {"protocol": "mycelium.replica_plan.v1", "route_ready": False}
    runtime = {"protocol": "mycelium.replica_runtime.v1", "requests": []}
    calls: list[str] = []
    route = SimpleNamespace(
        m18_replica_plan=lambda: calls.append("plan") or plan,
        m18_replica_runtime=lambda: calls.append("runtime") or runtime,
    )

    async def app(*_args):
        raise AssertionError("m18_projection_reached_asgi")

    server = create_server(
        app=app,
        route=route,
        static_root=static_root,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path, expected in (
            ("/__mycelium/m18-replica-plan", plan),
            ("/__mycelium/m18-replica-runtime", runtime),
        ):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == expected
        assert calls == ["plan", "runtime"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_m19_evidence_endpoints_are_read_only(tmp_path: Path) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    evidence = {
        "m19_liveness": {"protocol": "mycelium.m19_liveness.v1"},
        "m19_recovery_plan": {"protocol": "mycelium.m19_recovery_plan.v1"},
        "m19_recovery_runtime": {"protocol": "mycelium.m19_recovery_runtime.v1"},
    }
    calls: list[str] = []
    route = SimpleNamespace(
        **{
            name: (lambda key=name: calls.append(key) or evidence[key])
            for name in evidence
        }
    )

    async def app(*_args):
        raise AssertionError("m19_projection_reached_asgi")

    server = create_server(
        app=app, route=route, static_root=static_root, host="127.0.0.1", port=0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path, name in (
            ("/__mycelium/m19-liveness", "m19_liveness"),
            ("/__mycelium/m19-recovery-plan", "m19_recovery_plan"),
            ("/__mycelium/m19-recovery-runtime", "m19_recovery_runtime"),
        ):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == evidence[name]
        assert calls == list(evidence)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_m20_evidence_endpoints_are_read_only(tmp_path: Path) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    evidence = {
        "m20_speculative_plan": {"protocol": "mycelium.m20_speculative_plan.v1"},
        "m20_speculative_runtime": {
            "protocol": "mycelium.m20_speculative_runtime.v1"
        },
    }
    calls: list[str] = []
    route = SimpleNamespace(
        **{
            name: (lambda key=name: calls.append(key) or evidence[key])
            for name in evidence
        }
    )

    async def app(*_args):
        raise AssertionError("m20_projection_reached_asgi")

    server = create_server(
        app=app, route=route, static_root=static_root, host="127.0.0.1", port=0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path, name in (
            ("/__mycelium/m20-speculative-plan", "m20_speculative_plan"),
            ("/__mycelium/m20-speculative-runtime", "m20_speculative_runtime"),
        ):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == evidence[name]
        assert calls == list(evidence)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_optional_m13_projection_loader_fails_closed(tmp_path: Path) -> None:
    assert _placement_projection(tmp_path) is None
    fixture = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "compatibility-fixtures"
        / "m13-placement-projection-v1.json"
    )
    target = tmp_path / "m13-placement-projection.json"
    target.write_bytes(fixture.read_bytes())

    assert _placement_projection(tmp_path)["placement_provenance"] == "planner_v2"

    target.unlink()
    target.symlink_to(fixture)
    with pytest.raises(ValueError, match="unsafe"):
        _placement_projection(tmp_path)


def test_optional_m15_comparison_loader_fails_closed(tmp_path: Path) -> None:
    assert _workload_comparison(tmp_path) is None
    fixture = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "compatibility-fixtures"
        / "m15-plan-comparison-v1.json"
    )
    target = tmp_path / "m15-plan-comparison.json"
    target.write_bytes(fixture.read_bytes())

    assert (
        _workload_comparison(tmp_path)["protocol"] == "mycelium.m15_plan_comparison.v1"
    )

    target.unlink()
    target.symlink_to(fixture)
    with pytest.raises(ValueError, match="unsafe"):
        _workload_comparison(tmp_path)


def test_optional_m18_replica_plan_loader_fails_closed(tmp_path: Path) -> None:
    assert _m18_replica_plan(tmp_path) is None
    target = tmp_path / "m18-replica-plan.json"
    target.write_text('{"protocol":"wrong"}', encoding="utf-8")
    with pytest.raises(ValueError, match="m18_replica_plan_invalid"):
        _m18_replica_plan(tmp_path)

    target.unlink()
    target.symlink_to(Path(__file__))
    with pytest.raises(ValueError, match="m18_replica_plan_unsafe"):
        _m18_replica_plan(tmp_path)
