from __future__ import annotations

import asyncio
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

import mycelium_live.supervisor as supervisor_module
from mycelium_live.artifact_provisioner import ArtifactAcquisitionStore
from mycelium_live.route import FakeLiveRoute, PhysicalLiveRoute, RouteIdentity
from mycelium_live.supervisor import (
    LiveObservatoryApplication,
    LiveSwarmCoordinator,
    _explicit_historical_evidence,
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
from mycelium_ui_gateway.coordinator import CoordinatorError
from mycelium_ui_gateway.validation import validate_observatory_envelope
from physical_inference_qualification import ControllerError


def test_main_forwards_isolated_product_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        supervisor_module,
        "run_physical_server",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    product_state_root = tmp_path / "product-state"
    assert (
        supervisor_module.main(
            [
                "--operator-plan",
                str(tmp_path / "operator-plan.json"),
                "--seed-state-root",
                str(tmp_path / "seed"),
                "--product-state-root",
                str(product_state_root),
            ]
        )
        == 0
    )
    assert captured["product_state_root"] == product_state_root


def test_main_composes_a8_relay_authority_with_a5_product_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        supervisor_module,
        "run_physical_server",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    product_state_root = tmp_path / "combined-product-state"

    assert (
        supervisor_module.main(
            [
                "--operator-plan",
                str(tmp_path / "operator-plan.json"),
                "--seed-state-root",
                str(tmp_path / "seed"),
                "--product-state-root",
                str(product_state_root),
                "--a8-force-relay",
            ]
        )
        == 0
    )
    assert captured["force_relay"] is True
    assert captured["product_state_root"] == product_state_root


def test_supervisor_main_propagates_force_relay_to_single_and_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        supervisor_module,
        "run_physical_server",
        lambda **kwargs: calls.append(("single", kwargs["force_relay"])) or 0,
    )
    base = [
        "--operator-plan",
        str(tmp_path / "one.json"),
        "--seed-state-root",
        str(tmp_path / "seed"),
        "--a8-force-relay",
    ]
    assert supervisor_module.main(base) == 0
    assert calls == [("single", True)]

    calls.clear()
    monkeypatch.setattr(
        supervisor_module,
        "run_registry_server",
        lambda **kwargs: calls.append(("registry", kwargs["force_relay"])) or 0,
    )
    assert (
        supervisor_module.main(
            [
                *base,
                "--operator-plan",
                str(tmp_path / "two.json"),
            ]
        )
        == 0
    )
    assert calls == [("registry", True)]


def test_trusted_proxy_capability_loader_is_owner_private_and_exact(
    tmp_path: Path,
) -> None:
    capability = tmp_path / "proxy-capability"
    capability.write_bytes(b"a" * 64)
    capability.chmod(0o600)
    assert supervisor_module._load_trusted_proxy_capability(capability) == b"a" * 64

    capability.chmod(0o644)
    with pytest.raises(ValueError, match="invalid_trusted_proxy_capability"):
        supervisor_module._load_trusted_proxy_capability(capability)
    capability.chmod(0o600)

    for index, value in enumerate(
        (b"short", b"a" * 63, b"a" * 65, b"A" * 64, b"g" * 64, b"a" * 64 + b"\n")
    ):
        malformed = tmp_path / f"malformed-{index}"
        malformed.write_bytes(value)
        malformed.chmod(0o600)
        with pytest.raises(ValueError, match="invalid_trusted_proxy_capability"):
            supervisor_module._load_trusted_proxy_capability(malformed)

    link = tmp_path / "link"
    link.symlink_to(capability)
    with pytest.raises(ValueError, match="invalid_trusted_proxy_capability"):
        supervisor_module._load_trusted_proxy_capability(link)

    hardlink = tmp_path / "hardlink"
    os.link(capability, hardlink)
    with pytest.raises(ValueError, match="invalid_trusted_proxy_capability"):
        supervisor_module._load_trusted_proxy_capability(hardlink)

    real_root = tmp_path / "real-root"
    private = real_root / "private"
    private.mkdir(parents=True, mode=0o700)
    nested = private / "capability"
    nested.write_bytes(b"b" * 64)
    nested.chmod(0o600)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ValueError, match="invalid_trusted_proxy_capability"):
        supervisor_module._load_trusted_proxy_capability(
            linked_root / "private" / "capability"
        )


def test_force_relay_reaches_route_factory_in_single_and_registry_loaders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def reject(_plan: Path, **kwargs: object) -> object:
        calls.append(kwargs["force_relay"] is True)
        raise RuntimeError("stop_after_factory")

    monkeypatch.setattr(
        supervisor_module.PhysicalLiveRoute,
        "from_operator_plan",
        reject,
    )
    with pytest.raises(RuntimeError, match="stop_after_factory"):
        supervisor_module.run_physical_server(
            operator_plan=tmp_path / "plan.json",
            host="127.0.0.1",
            port=8787,
            seed_state_root=tmp_path / "seed",
            force_relay=True,
        )
    with pytest.raises(RuntimeError, match="stop_after_factory"):
        supervisor_module._qualified_runtime(
            tmp_path / "plan.json",
            seed_state_root=tmp_path / "seed",
            force_relay=True,
        )
    assert calls == [True, True]


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


def test_artifact_acquisition_endpoint_projects_durable_provisioner_ledger(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok")
    store = ArtifactAcquisitionStore(tmp_path / "artifact-state")

    async def app(*_args):
        raise AssertionError("artifact_acquisition_endpoint_reached_asgi")

    server = create_server(
        app=app,
        route=SimpleNamespace(),
        static_root=static_root,
        host="127.0.0.1",
        port=0,
        artifact_acquisition_store=store,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/__mycelium/artifacts/acquisitions")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {
            "protocol": "mycelium.swarm_artifact_acquisition_ledger.v1",
            "generation": 0,
            "current": None,
            "history": [],
        }
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


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


def test_live_swarm_coordinator_mints_target_device_bundle() -> None:
    class MembershipSource:
        def membership_status(self, *, qualification):
            assert qualification == "qualified"
            return {
                "protocol": "mycelium.product_ui.swarm.v1",
                "native_nodes": [],
                "browser_workers": [],
            }

        def mint_native_invite(self, *, seed_url, ttl_seconds, nonce):
            assert seed_url == "https://seed.example.test"
            assert ttl_seconds == 300
            assert nonce.startswith("product-ui-")
            return {"protocol": "mycelium.invite_bundle.v1", "token": "signed-token"}

    coordinator = LiveSwarmCoordinator(
        MembershipSource(),
        SimpleNamespace(current=lambda: "qualified"),
        seed_url="https://seed.example.test",
    )

    result = coordinator.create_invite(
        {
            "protocol": "mycelium.product_ui.swarm.v1",
            "action": "create_invite",
            "capability": "native_inference_node",
            "expires_in_seconds": 300,
        }
    )

    assert result["capability"] == "native_inference_node"
    assert json.loads(result["invite_code"])["token"] == "signed-token"
    assert result["invite_id"].startswith("product-ui-")


def test_live_swarm_coordinator_rejects_in_browser_join() -> None:
    source = SimpleNamespace(membership_status=lambda **_kwargs: {})
    coordinator = LiveSwarmCoordinator(
        source,
        SimpleNamespace(current=lambda: None),
        seed_url="https://seed.example.test",
    )

    with pytest.raises(CoordinatorError) as caught:
        coordinator.join({})

    assert getattr(caught.value, "code", None) == "join_on_target_device_required"


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


def test_qualification_renewal_prefers_exact_startup_challenge_path(
    monkeypatch,
) -> None:
    qualification = object()

    class RenewableRoute:
        startup_challenge = ((1, 2), (3, 4))

        def __init__(self) -> None:
            self.released = []
            self.request_id = None

        def infer_startup_challenge(self, *, request_id, sink):
            del sink
            self.request_id = request_id
            return SimpleNamespace(token_ids=(3, 4))

        def infer(self, *_args, **_kwargs):
            raise AssertionError("renewal used product-filtered inference")

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


def test_physical_startup_challenge_uses_request_local_empty_stop_set(
    monkeypatch,
) -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._plan = {
        "request": {"prompt_token_ids": [1, 2]},
        "expected_token_ids": [3, 151645, 4],
    }
    captured = {}
    expected = SimpleNamespace(token_ids=(3, 151645, 4))

    def infer(token_ids, **kwargs):
        captured["token_ids"] = token_ids
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(route, "infer", infer)
    sink = object()

    assert (
        route.infer_startup_challenge(request_id="startup-renewal", sink=sink)
        is expected
    )
    assert captured == {
        "token_ids": (1, 2),
        "max_new_tokens": 3,
        "request_id": "startup-renewal",
        "sink": sink,
        "_stop_token_ids_override": frozenset(),
    }


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


def test_physical_server_surfaces_safe_open_remote_code(monkeypatch) -> None:
    class RejectingRoute:
        execution_graph = _graph(("node-a", "node-b", "node-c"))

        def __init__(self) -> None:
            self.closed = False
            self.cleaned = False

        def open(self):
            raise ControllerError(
                "node_command_rejected",
                remote_code="invalid_stage_pack_file",
            )

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
        match="startup_route_open_rejected:invalid_stage_pack_file",
    ):
        run_physical_server(
            operator_plan=SimpleNamespace(),
            host="127.0.0.1",
            port=8788,
            seed_state_root=Path("/private/test-seed"),
        )
    assert route.closed is True
    assert route.cleaned is True


def test_physical_server_logs_node_stderr_diagnostic_before_cleanup_mask(
    monkeypatch, capsys
) -> None:
    class RejectingRoute:
        execution_graph = _graph(("node-a", "node-b"))

        def __init__(self) -> None:
            self.closed = False
            self.cleaned = False

        def open(self):
            raise ControllerError("physical_run_cleanup_failed") from ControllerError(
                "node_command_rejected",
                remote_code="node_command_failed",
                diagnostic=(
                    "node node-b stderr (tail):\n"
                    "Traceback (most recent call last):\n"
                    "ValueError: stage pack digest mismatch\n"
                ),
            )

        def close(self) -> None:
            self.closed = True

        def cleanup(self) -> None:
            self.cleaned = True

    route = RejectingRoute()
    monkeypatch.setattr(
        "mycelium_live.supervisor.PhysicalLiveRoute.from_operator_plan",
        lambda _plan, **_kwargs: route,
    )

    with pytest.raises(RuntimeError, match="startup_route_open_rejected"):
        run_physical_server(
            operator_plan=SimpleNamespace(),
            host="127.0.0.1",
            port=8788,
            seed_state_root=Path("/private/test-seed"),
        )

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "serve log must carry the open diagnostic before the cleanup mask"
    document = json.loads(lines[0])
    assert document["protocol"] == "mycelium.live_serve_diagnostic.v1"
    assert document["stage"] == "route_open"
    assert isinstance(document["emitted_at_unix_ms"], int)
    chain = document["cause_chain"]
    assert [entry["code"] for entry in chain] == [
        "physical_run_cleanup_failed",
        "node_command_failed",
    ]
    assert chain[0]["type"] == "ControllerError"
    assert "node_stderr_tail" not in chain[0]
    assert "stage pack digest mismatch" in chain[1]["node_stderr_tail"]

    assert route.closed is True
    assert route.cleaned is True


def test_physical_server_unmasks_cleanup_failure_and_logs_node_stderr(
    monkeypatch, capsys
) -> None:
    from mycelium_node.process import NodeProcessError

    class RejectingRoute:
        execution_graph = _graph(("node-a", "node-b"))

        def __init__(self) -> None:
            self.closed = False
            self.cleaned = False

        def open(self):
            raise NodeProcessError(
                "node_command_failed",
                detail=(
                    "node node-2 stderr (tail):\n"
                    "ValueError: stage pack digest mismatch\n"
                ),
            )

        def close(self) -> None:
            self.closed = True

        def cleanup(self) -> None:
            raise ControllerError("physical_cleanup_failed")

    route = RejectingRoute()
    monkeypatch.setattr(
        "mycelium_live.supervisor.PhysicalLiveRoute.from_operator_plan",
        lambda _plan, **_kwargs: route,
    )

    with pytest.raises(
        RuntimeError,
        match="startup_route_open_rejected:node_command_failed",
    ):
        run_physical_server(
            operator_plan=SimpleNamespace(),
            host="127.0.0.1",
            port=8788,
            seed_state_root=Path("/private/test-seed"),
        )

    documents = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert len(documents) == 2
    open_document = documents[0]
    assert open_document["stage"] == "route_open"
    assert open_document["cause_chain"][0]["code"] == "node_command_failed"
    assert "stage pack digest mismatch" in open_document["cause_chain"][0][
        "node_stderr_tail"
    ]
    # The cleanup failure is logged, not raised over the open rejection:
    # the public error stays the real startup reason.
    cleanup_document = documents[1]
    assert cleanup_document["stage"] == "route_cleanup"
    assert cleanup_document["cause_chain"][0]["code"] == "physical_cleanup_failed"
    assert route.closed is True


def test_exception_chain_document_is_bounded_and_cycle_safe() -> None:
    from mycelium_live.supervisor import _exception_chain_document

    inner = ValueError("inner")
    outer = ControllerError(
        "node_command_rejected",
        remote_code="node_command_failed",
        diagnostic="tail-" + "x" * 40_000,
    )
    outer.__cause__ = inner  # plain chain: controller -> value error
    inner.__context__ = outer  # artificially cyclic; walk must terminate

    chain = _exception_chain_document(outer)
    assert [entry["type"] for entry in chain] == ["ControllerError", "ValueError"]
    assert len(chain[0]["node_stderr_tail"]) <= 16_000


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


def test_loopback_wrapper_trusts_only_exact_https_forwarding_mark_when_enabled(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    scopes: list[dict] = []

    async def app(scope, _receive, send):
        scopes.append(dict(scope))
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    server = create_server(
        app=app,
        route=SimpleNamespace(),
        static_root=static_root,
        host="127.0.0.1",
        port=0,
        trusted_https_proxy=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for marker in ("https", "https,http", None):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            headers = {"Host": "a8.example.test"}
            if marker is not None:
                headers["X-Forwarded-Proto"] = marker
            connection.request("GET", "/api/v1/bootstrap", headers=headers)
            response = connection.getresponse()
            assert response.status == 204
            response.read()
            connection.close()

        assert [scope["scheme"] for scope in scopes] == ["https", "http", "http"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
        connection.request("GET", "/__mycelium/swarm/resource-observations")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == expected
        assert calls == ["snapshot"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_runtime_and_historical_evidence_endpoints_are_separate(tmp_path: Path) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    from mycelium_evidence import EvidenceProjectionRegistry, sealed_evidence_projection

    route_status = {
        "protocol": "mycelium.live_route_status.v1",
        "route_alive": True,
        "counters": {"frames_sent": 4, "fatal": None},
    }
    history = sealed_evidence_projection(
        record_id="replication-plan-a",
        capability="replicated_serving",
        authority="planner",
        generation=1,
        observed_at_unix_ms=1_000,
        payload={"protocol": "mycelium.replica_plan.v1", "route_ready": False},
    )
    registry = EvidenceProjectionRegistry(
        runtime_source=lambda: route_status,
        historical_records=[history],
        clock_unix_ms=lambda: 2_000,
        incarnation="test",
    )

    async def app(*_args):
        raise AssertionError("evidence_projection_reached_asgi")

    server = create_server(
        app=app,
        route=SimpleNamespace(),
        static_root=static_root,
        host="127.0.0.1",
        port=0,
        evidence_registry=registry,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in (
            "/__mycelium/evidence/runtime",
            "/__mycelium/evidence/history?capability=replicated_serving",
        ):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200
            document = json.loads(response.read())
            if path.endswith("runtime"):
                assert document["source_kind"] == "live_runtime"
                assert document["freshness"] == "current"
            else:
                assert document["records"] == [history]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_milestone_numbered_evidence_endpoints_are_retired(tmp_path: Path) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")

    async def app(*_args):
        raise AssertionError("retired_endpoint_reached_asgi")

    server = create_server(
        app=app,
        route=SimpleNamespace(),
        static_root=static_root,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in (
            "/__mycelium/m18-replica-plan",
            "/__mycelium/m19-liveness",
            "/__mycelium/m20-speculative-plan",
            "/__mycelium/m21-heterogeneous",
            "/__mycelium/m22-release",
            "/__mycelium/m23-kv",
            "/__mycelium/m15-plan-comparison",
            "/__mycelium/m16-runtime-status",
            "/__mycelium/m17-model-operation",
            "/__mycelium/m17-swarm-evidence",
        ):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 404
            assert json.loads(response.read()) == {"error": "product_endpoint_unknown"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_deployment_activation_endpoints_are_same_origin_and_closed_shape(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    status = {
        "protocol": "mycelium.deployment_activation.v1",
        "generation": 1,
        "busy_candidate_id": None,
        "invalid_candidate_count": 0,
        "candidates": [],
    }
    calls: list[str] = []
    activation = SimpleNamespace(
        status=lambda: calls.append("status") or status,
        activate=lambda candidate_id: calls.append(candidate_id) or status,
        unload=lambda candidate_id: calls.append(f"unload:{candidate_id}") or status,
    )

    async def app(*_args):
        raise AssertionError("activation_endpoint_reached_asgi")

    server = create_server(
        app=app,
        route=SimpleNamespace(),
        static_root=static_root,
        host="127.0.0.1",
        port=0,
        activation=activation,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/__mycelium/deployment-activation")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == status

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/deployment-activation/unload",
            body=json.dumps({"candidate_id": "deployment-candidate", "force": True}),
            headers={
                "content-type": "application/json",
                "origin": f"http://127.0.0.1:{server.server_port}",
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {"error": "invalid_activation_request"}

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/deployment-activation/unload",
            body=json.dumps({"candidate_id": "deployment-candidate"}),
            headers={"content-type": "application/json", "origin": "https://evil.test"},
        )
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read()) == {"error": "origin_mismatch"}

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/deployment-activation/unload",
            body=json.dumps({"candidate_id": "deployment-candidate"}),
            headers={
                "content-type": "application/json",
                "origin": f"http://127.0.0.1:{server.server_port}",
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == status

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/deployment-activation/start",
            body=json.dumps({"candidate_id": "deployment-candidate"}),
            headers={
                "content-type": "application/json",
                "origin": f"http://127.0.0.1:{server.server_port}",
            },
        )
        response = connection.getresponse()
        assert response.status == 202
        assert json.loads(response.read()) == status

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/deployment-activation/start",
            body=json.dumps({"candidate_id": "deployment-candidate", "path": "/tmp"}),
            headers={
                "content-type": "application/json",
                "origin": f"http://127.0.0.1:{server.server_port}",
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {"error": "invalid_activation_request"}

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/deployment-activation/start",
            body=json.dumps({"candidate_id": "deployment-candidate"}),
            headers={"content-type": "application/json", "origin": "https://evil.test"},
        )
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read()) == {"error": "origin_mismatch"}
        assert calls == [
            "status",
            "unload:deployment-candidate",
            "deployment-candidate",
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_model_capacity_refresh_endpoints_are_same_origin_and_closed_shape(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    status = {
        "protocol": "mycelium.model_capacity_refresh.v1",
        "generation": 1,
        "state": "idle",
    }
    calls: list[str] = []
    refresh = SimpleNamespace(
        status=lambda: calls.append("status") or status,
        start=lambda: calls.append("start") or status,
    )

    async def app(*_args):
        raise AssertionError("capacity_refresh_endpoint_reached_asgi")

    server = create_server(
        app=app,
        route=SimpleNamespace(),
        static_root=static_root,
        host="127.0.0.1",
        port=0,
        capacity_refresh=refresh,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/__mycelium/model-capacity-refresh")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == status

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/model-capacity-refresh/start",
            body="{}",
            headers={
                "content-type": "application/json",
                "origin": f"http://127.0.0.1:{server.server_port}",
            },
        )
        response = connection.getresponse()
        assert response.status == 202
        assert json.loads(response.read()) == status

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/model-capacity-refresh/start",
            body=json.dumps({"cache_root": "/private/path"}),
            headers={
                "content-type": "application/json",
                "origin": f"http://127.0.0.1:{server.server_port}",
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {
            "error": "invalid_capacity_refresh_request"
        }
        assert calls == ["status", "start"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_model_preparation_endpoints_accept_only_public_identity(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    status = {
        "protocol": "mycelium.model_preparation.v1",
        "generation": 1,
        "state": "idle",
    }
    calls: list[object] = []
    preparation = SimpleNamespace(
        status=lambda: calls.append("status") or status,
        start=lambda decision: calls.append(decision) or status,
        reacquire=lambda decision, candidate_id: calls.append(
            ("reacquire", candidate_id, decision)
        )
        or status,
    )

    async def app(*_args):
        raise AssertionError("model_preparation_endpoint_reached_asgi")

    server = create_server(
        app=app,
        route=SimpleNamespace(),
        static_root=static_root,
        host="127.0.0.1",
        port=0,
        model_preparation=preparation,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    revision = "b" * 40
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/__mycelium/model-preparation")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == status

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/model-preparation/start",
            body=json.dumps(
                {
                    "decision": {
                        "protocol": "mycelium.model_representation_decision.v1",
                        "model_id": "Qwen/Qwen3-8B",
                        "revision": revision,
                        "source_quantization": "bfloat16",
                        "serving_dtype": "float32",
                        "serving_quantization": "int8-weight-only",
                        "representation_digest": "sha256:" + "d" * 64,
                        "conversion_authorized": True,
                    }
                }
            ),
            headers={
                "content-type": "application/json",
                "origin": f"http://127.0.0.1:{server.server_port}",
            },
        )
        response = connection.getresponse()
        assert response.status == 202
        assert json.loads(response.read()) == status

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/model-preparation/reacquire",
            body=json.dumps(
                {
                    "candidate_id": "candidate-1",
                    "decision": {
                        "protocol": "mycelium.model_representation_decision.v1",
                        "model_id": "Qwen/Qwen3-8B",
                        "revision": revision,
                        "source_quantization": "bfloat16",
                        "serving_dtype": "float32",
                        "serving_quantization": "int8-weight-only",
                        "representation_digest": "sha256:" + "d" * 64,
                        "conversion_authorized": True,
                    },
                }
            ),
            headers={
                "content-type": "application/json",
                "origin": f"http://127.0.0.1:{server.server_port}",
            },
        )
        response = connection.getresponse()
        assert response.status == 202
        assert json.loads(response.read()) == status

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/__mycelium/model-preparation/start",
            body=json.dumps(
                {
                    "decision": {},
                    "snapshot_path": "/private/model",
                }
            ),
            headers={
                "content-type": "application/json",
                "origin": f"http://127.0.0.1:{server.server_port}",
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {
            "error": "invalid_model_preparation_request"
        }
        assert calls == [
            "status",
            {
                "protocol": "mycelium.model_representation_decision.v1",
                "model_id": "Qwen/Qwen3-8B",
                "revision": revision,
                "source_quantization": "bfloat16",
                "serving_dtype": "float32",
                "serving_quantization": "int8-weight-only",
                "representation_digest": "sha256:" + "d" * 64,
                "conversion_authorized": True,
            },
            (
                "reacquire",
                "candidate-1",
                {
                    "protocol": "mycelium.model_representation_decision.v1",
                    "model_id": "Qwen/Qwen3-8B",
                    "revision": revision,
                    "source_quantization": "bfloat16",
                    "serving_dtype": "float32",
                    "serving_quantization": "int8-weight-only",
                    "representation_digest": "sha256:" + "d" * 64,
                    "conversion_authorized": True,
                },
            ),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_governance_readiness_endpoint_serves_only_the_frozen_projection(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")
    projection = {
        "protocol": "mycelium.governance_readiness.v1",
        "release_ready": False,
        "release_exclusions": ["runtime gates remain open"],
    }

    async def app(*_args):
        raise AssertionError("governance_endpoint_reached_asgi")

    server = create_server(
        app=app,
        route=SimpleNamespace(),
        static_root=static_root,
        host="127.0.0.1",
        port=0,
        governance_projection=projection,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/__mycelium/governance-readiness")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == projection
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_explicit_historical_evidence_keeps_intrinsic_capture_time() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "compatibility-fixtures"
        / "m23-kv-gate-v1.json"
    )
    records = _explicit_historical_evidence((path,))
    assert len(records) == 1
    assert records[0]["source_kind"] == "sealed_historical"
    assert records[0]["freshness"] == "historical"
    assert (
        records[0]["observed_at_unix_ms"]
        == records[0]["payload"]["generated_at_unix_ms"]
    )


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


# ---------------------------------------------------------------------------
# A5 runtime replica-qualification install surface (operator-scoped, no
# request-path contract change; baseline = empty set -> incumbent rotation)
# ---------------------------------------------------------------------------


class _RecordingQualificationRoute:
    """Records what the install endpoint hands to the route method."""

    def __init__(self) -> None:
        self.installs: list[list[dict]] = []

    def set_replica_track_qualification(self, documents) -> None:
        self.installs.append([dict(document) for document in documents])


def _install_server(tmp_path: Path, route=None):
    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "index.html").write_text("ok", encoding="utf-8")

    async def app(*_args):
        raise AssertionError("replica_qualification_endpoint_reached_asgi")

    server = create_server(
        app=app,
        route=route if route is not None else _RecordingQualificationRoute(),
        static_root=static_root,
        host="127.0.0.1",
        port=0,
        artifact_acquisition_store=ArtifactAcquisitionStore(
            tmp_path / "artifact-state"
        ),
        replica_operator_token="test-a5-operator-token-000000000000",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _post_install(
    server,
    *,
    origin: str | None,
    payload: object,
    operator_token: str | None = "test-a5-operator-token-000000000000",
) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    if operator_token is not None:
        headers["Authorization"] = f"Bearer {operator_token}"
    body = json.dumps(payload)
    connection.request(
        "POST",
        "/__mycelium/replica-qualification/install",
        body=body,
        headers=headers,
    )
    response = connection.getresponse()
    document = json.loads(response.read())
    connection.close()
    return response.status, document


def _valid_replica_qualification() -> dict:
    from mycelium_replica_contracts import compatibility_fixtures

    return compatibility_fixtures()["replica-qualification-v1.json"]


def test_replica_qualification_install_clears_and_restores(tmp_path: Path) -> None:
    """Baseline (empty) and candidate (validated doc) installs both land."""

    server, thread = _install_server(tmp_path)
    route = server.route
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        status, document = _post_install(server, origin=origin, payload={"documents": []})
        assert (status, document) == (200, {"installed": 0})
        assert route.installs[-1] == []

        valid = _valid_replica_qualification()
        status, document = _post_install(
            server, origin=origin, payload={"documents": [valid]}
        )
        assert (status, document) == (200, {"installed": 1})
        assert route.installs[-1] == [valid]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_replica_qualification_install_rejects_bad_payloads(tmp_path: Path) -> None:
    server, thread = _install_server(tmp_path)
    route = server.route
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        # Missing Origin -> operator boundary holds.
        status, document = _post_install(server, origin=None, payload={"documents": []})
        assert (status, document) == (403, {"error": "origin_mismatch"})
        status, document = _post_install(
            server,
            origin=origin,
            payload={"documents": []},
            operator_token="",
        )
        assert (status, document) == (
            403,
            {"error": "operator_authorization_required"},
        )
        # Wrong shape -> 400.
        status, document = _post_install(
            server, origin=origin, payload={"documents": "not-a-list"}
        )
        assert (status, document) == (400, {"error": "invalid_replica_qualification"})
        # Structurally invalid qualification -> 400, nothing installed.
        broken = _valid_replica_qualification()
        broken["qualification_digest"] = "not-a-digest"
        status, document = _post_install(
            server, origin=origin, payload={"documents": [broken]}
        )
        assert (status, document) == (400, {"error": "invalid_replica_qualification"})
        assert route.installs == []
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_replica_qualification_install_unavailable_without_route_method(
    tmp_path: Path,
) -> None:
    server, thread = _install_server(tmp_path, route=SimpleNamespace())
    try:
        status, document = _post_install(
            server,
            origin=f"http://127.0.0.1:{server.server_port}",
            payload={"documents": []},
        )
        assert (status, document) == (404, {"error": "replica_qualification_unavailable"})
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
