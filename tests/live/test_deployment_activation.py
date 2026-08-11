from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_live.activation import (
    DeploymentActivationError,
    PreparedDeploymentActivation,
    validate_activation_status,
)
from mycelium_live.registry import LiveDeploymentRegistry, QualifiedDeploymentRuntime
from mycelium_live.route import FakeLiveRoute
from mycelium_router.serialization import execution_graph_from_dict
from tests.physical_runner.conftest import operator_plan_payload, write_operator_plan


ROOT = Path(__file__).resolve().parents[2]


class _Codec:
    def encode(self, _prompt: str) -> tuple[int, ...]:
        return (1,)

    def decode_token(self, token_id: int) -> str:
        return str(token_id)


def _runtime(graph) -> QualifiedDeploymentRuntime:
    route = FakeLiveRoute(scripted_tokens=(2,))
    route.open()
    return QualifiedDeploymentRuntime(
        deployment_id=graph.deployment_id,
        model_id=graph.model_id,
        model_revision=graph.resolved_commit,
        quantization="int8-weight-only",
        qualified_at_unix_ms=1_800_000_000_000,
        route=route,
        graph=graph,
        codec=_Codec(),
        qualification=SimpleNamespace(
            route_ready=True,
            qualification_id=f"qualification-{graph.deployment_id}",
            issued_at_unix_ms=1_800_000_000_000,
            deployment_id=graph.deployment_id,
            model_id=graph.model_id,
        ),
    )


def _graph_document(*, deployment_id: str | None = None) -> dict:
    document = json.loads(
        (ROOT / "contracts/compatibility-fixtures/execution-graph-v1.json").read_text()
    )
    if deployment_id is not None:
        document["deployment_id"] = deployment_id
    return document


def _plan(candidate_root: Path, workspace: Path, graph: dict, name: str) -> Path:
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    payload = operator_plan_payload(workspace)
    physical_graph = json.loads(json.dumps(graph))
    for stage in physical_graph["stages"]:
        stage["layer_range"] = stage.pop("range")
    payload["controller"]["run_plan"]["deployment_id"] = graph["deployment_id"]
    payload["controller"]["run_plan"]["nodes"] = [
        {
            "node_id": "node-a",
            "endpoint_secret_file": "/opt/mycelium/identities/node-a/endpoint",
            "configure": {"graph": physical_graph},
        },
        {
            "node_id": "node-b",
            "endpoint_secret_file": "/opt/mycelium/identities/node-b/endpoint",
            "configure": {"graph": physical_graph},
        },
    ]
    return write_operator_plan(candidate_root / name, payload)


def _activation(
    tmp_path: Path,
    loader,
) -> tuple[PreparedDeploymentActivation, object, Path, LiveDeploymentRegistry]:
    candidate_root = tmp_path / "candidates"
    state_root = tmp_path / "state"
    candidate_root.mkdir(mode=0o700)
    state_root.mkdir(mode=0o700)
    graph_document = _graph_document()
    graph = execution_graph_from_dict(graph_document)
    incumbent_document = _graph_document(
        deployment_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    incumbent = _runtime(execution_graph_from_dict(incumbent_document))
    _plan(candidate_root, tmp_path / "workspace", graph_document, "candidate.json")
    registry = LiveDeploymentRegistry([incumbent])
    activation = PreparedDeploymentActivation(
        candidate_root=candidate_root,
        state_root=state_root,
        registry=registry,
        runtime_loader=loader,
    )
    return activation, graph, state_root, registry


def _wait_terminal(activation: PreparedDeploymentActivation) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = dict(activation.status())
        if status["busy_candidate_id"] is None:
            return status
        time.sleep(0.01)
    raise AssertionError("activation did not finish")


def test_prepared_candidate_qualifies_without_selecting_or_exposing_paths(
    tmp_path: Path,
) -> None:
    released = threading.Event()
    graph_holder = {}

    def loader(plan: Path, progress):
        assert plan.parent.name == "candidate-plans"
        assert plan.stat().st_mode & 0o077 == 0
        progress("opening_route")
        released.wait(timeout=2)
        progress("qualifying_route")
        return _runtime(graph_holder["graph"])

    activation, graph, state_root, registry = _activation(tmp_path, loader)
    graph_holder["graph"] = graph
    prepared = activation.status()
    candidate = prepared["candidates"][0]
    assert candidate["state"] == "prepared"
    assert str(tmp_path) not in json.dumps(prepared)

    accepted = activation.activate(candidate["candidate_id"])
    assert accepted["busy_candidate_id"] == candidate["candidate_id"]
    assert accepted["candidates"][0]["state"] == "activating"
    released.set()
    terminal = _wait_terminal(activation)

    assert terminal["candidates"][0]["state"] == "qualified"
    assert terminal["candidates"][0]["completed_steps"] == 4
    assert terminal["busy_candidate_id"] is None
    assert list((state_root / "candidate-plans").glob("*.json"))
    assert registry.registry_status()["selected_deployment_id"] != graph.deployment_id


def test_qualified_candidate_can_be_unloaded_and_reactivated(tmp_path: Path) -> None:
    graph_holder = {}

    def loader(_plan: Path, progress):
        progress("opening_route")
        progress("qualifying_route")
        return _runtime(graph_holder["graph"])

    activation, graph, _state_root, registry = _activation(tmp_path, loader)
    graph_holder["graph"] = graph
    candidate_id = activation.status()["candidates"][0]["candidate_id"]
    activation.activate(candidate_id)
    assert _wait_terminal(activation)["candidates"][0]["state"] == "qualified"

    unloaded = activation.unload(candidate_id)

    assert unloaded["candidates"][0]["state"] == "prepared"
    assert all(
        item["deployment_id"] != graph.deployment_id
        for item in registry.registry_status()["deployments"]
    )
    activation.activate(candidate_id)
    assert _wait_terminal(activation)["candidates"][0]["state"] == "qualified"


def test_activation_failure_is_retryable_and_keeps_incumbent(tmp_path: Path) -> None:
    graph_holder = {}
    fail = True

    def loader(_plan: Path, progress):
        progress("opening_route")
        if fail:
            raise DeploymentActivationError("startup_challenge_failed")
        return _runtime(graph_holder["graph"])

    activation, graph, _state_root, registry = _activation(tmp_path, loader)
    graph_holder["graph"] = graph
    candidate_id = activation.status()["candidates"][0]["candidate_id"]
    incumbent = registry.registry_status()["selected_deployment_id"]

    activation.activate(candidate_id)
    failed = _wait_terminal(activation)
    assert failed["candidates"][0]["state"] == "failed"
    assert failed["candidates"][0]["reason_code"] == "startup_challenge_failed"
    assert registry.registry_status()["selected_deployment_id"] == incumbent

    fail = False
    activation.activate(candidate_id)
    assert _wait_terminal(activation)["candidates"][0]["state"] == "qualified"


def test_activation_rejects_unsafe_root_and_graph_disagreement(tmp_path: Path) -> None:
    root = tmp_path / "candidates"
    state = tmp_path / "state"
    root.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    graph = _graph_document()
    path = _plan(root, tmp_path / "workspace", graph, "candidate.json")
    payload = json.loads(path.read_text())
    payload["controller"]["run_plan"]["nodes"][1]["configure"]["graph"]["model_id"] = (
        "Qwen/different"
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    incumbent = _runtime(
        execution_graph_from_dict(
            _graph_document(deployment_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        )
    )
    activation = PreparedDeploymentActivation(
        candidate_root=root,
        state_root=state,
        registry=LiveDeploymentRegistry([incumbent]),
        runtime_loader=lambda *_args: pytest.fail("invalid candidate must not load"),
    )
    assert activation.status()["candidates"] == []
    assert activation.status()["invalid_candidate_count"] == 1

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(DeploymentActivationError, match="candidate_plan_root_unsafe"):
        PreparedDeploymentActivation(
            candidate_root=unsafe,
            state_root=state,
            registry=LiveDeploymentRegistry([incumbent]),
            runtime_loader=lambda *_args: incumbent,
        )


def test_activation_status_contract_is_closed() -> None:
    fixture = json.loads(
        (
            ROOT / "contracts/compatibility-fixtures/deployment-activation-v1.json"
        ).read_text()
    )
    validate_activation_status(fixture)

    fixture["candidates"][0]["private_plan_path"] = "/private/candidate.json"
    with pytest.raises(ValueError, match="deployment_activation_candidate_invalid"):
        validate_activation_status(fixture)
