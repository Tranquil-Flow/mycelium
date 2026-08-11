from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_live.health import LIVE_QUALIFICATION_REFRESH_AFTER_MS
from mycelium_live.registry import (
    DeploymentSelectionError,
    LiveDeploymentRegistry,
    QualifiedDeploymentRuntime,
)
from mycelium_live.route import FakeLiveRoute


class _Codec:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def encode(self, _prompt: str) -> tuple[int, ...]:
        return (1,)

    def decode_token(self, token_id: int) -> str:
        return f"{self.prefix}{token_id}"


def _runtime(index: int) -> QualifiedDeploymentRuntime:
    route = FakeLiveRoute(scripted_tokens=(index + 1,))
    route.open()
    return QualifiedDeploymentRuntime(
        deployment_id=f"deployment-{index}",
        model_id=f"Qwen/model-{index}",
        model_revision=f"{index}" * 40,
        quantization="int8-weight-only",
        qualified_at_unix_ms=1_000 + index,
        route=route,
        graph=SimpleNamespace(deployment_id=f"deployment-{index}", stages=(1, 2)),
        codec=_Codec(f"model-{index}:"),
        qualification=SimpleNamespace(
            route_ready=True,
            qualification_id=f"qualification-{index}",
            issued_at_unix_ms=1_000 + index,
            deployment_id=f"deployment-{index}",
            model_id=f"Qwen/model-{index}",
        ),
    )


def test_registry_lists_and_atomically_selects_qualified_deployments() -> None:
    registry = LiveDeploymentRegistry([_runtime(0), _runtime(1)])

    assert registry.current_deployment().deployment_id == "deployment-0"
    selected = registry.select("deployment-1")

    assert registry.current_deployment().deployment_id == "deployment-1"
    assert selected["selected_deployment_id"] == "deployment-1"
    assert [item["health"] for item in selected["deployments"]] == [
        "qualified",
        "qualified",
    ]


def test_registry_starts_with_one_runtime_and_adds_qualified_standby() -> None:
    incumbent = _runtime(0)
    candidate = _runtime(1)
    registry = LiveDeploymentRegistry([incumbent])

    status = registry.add_qualified_runtime(candidate)

    assert status["selected_deployment_id"] == "deployment-0"
    assert [item["deployment_id"] for item in status["deployments"]] == [
        "deployment-0",
        "deployment-1",
    ]
    assert registry.current_deployment().deployment_id == "deployment-0"
    registry.select("deployment-1")
    assert registry.current_deployment().deployment_id == "deployment-1"


def test_registry_rejects_duplicate_or_unqualified_runtime_insertion() -> None:
    incumbent = _runtime(0)
    registry = LiveDeploymentRegistry([incumbent])

    with pytest.raises(DeploymentSelectionError, match="deployment_duplicate"):
        registry.add_qualified_runtime(incumbent)

    unavailable = _runtime(1)
    unavailable.route.close()
    with pytest.raises(DeploymentSelectionError, match="deployment_not_qualified"):
        registry.add_qualified_runtime(unavailable)


def test_candidate_canary_executes_without_changing_selection() -> None:
    runtimes = [_runtime(0), _runtime(1)]
    runtimes[1].graph.stages = (
        SimpleNamespace(
            stage_id="stage-candidate",
            placements=(SimpleNamespace(node_id="fake-node"),),
        ),
    )
    registry = LiveDeploymentRegistry(runtimes)

    result = registry.canary_candidate(
        "deployment-1",
        case_id="arbitrary-capital",
        prompt="What is the capital of France?",
        max_new_tokens=1,
    )

    assert result["candidate_deployment_id"] == "deployment-1"
    assert result["output_text"] == "model-1:2"
    assert result["frames_per_request_by_stage"] == {"stage-candidate": 2}
    assert registry.registry_status()["selected_deployment_id"] == "deployment-0"


def test_candidate_canary_rejects_selected_or_invalid_input() -> None:
    registry = LiveDeploymentRegistry([_runtime(0), _runtime(1)])

    with pytest.raises(DeploymentSelectionError, match="candidate_already_selected"):
        registry.canary_candidate(
            "deployment-0",
            case_id="selected",
            prompt="test",
            max_new_tokens=1,
        )
    with pytest.raises(DeploymentSelectionError, match="candidate_canary_invalid"):
        registry.canary_candidate(
            "deployment-1",
            case_id="invalid",
            prompt="",
            max_new_tokens=1,
        )


def test_registry_persists_atomic_public_selection_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "deployments.json"
    registry = LiveDeploymentRegistry(
        [_runtime(0), _runtime(1)],
        state_path=state_path,
    )

    registry.select("deployment-1")

    persisted = json.loads(state_path.read_text())
    assert persisted == registry.registry_status()
    assert persisted["deployments"][1]["qualification_id"] == "qualification-1"
    assert not list(state_path.parent.glob("*.tmp"))


def test_registry_restores_last_qualified_selection_after_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "deployments.json"
    first = LiveDeploymentRegistry([_runtime(0), _runtime(1)], state_path=state_path)
    first.select("deployment-1")

    restarted = LiveDeploymentRegistry(
        [_runtime(0), _runtime(1)],
        state_path=state_path,
    )

    assert restarted.current_deployment().deployment_id == "deployment-1"
    assert restarted.registry_status()["selected_deployment_id"] == "deployment-1"


def test_registry_rejects_switch_until_request_is_fully_released() -> None:
    registry = LiveDeploymentRegistry([_runtime(0), _runtime(1)])
    assert registry.encode("test") == (1,)
    request = SimpleNamespace(
        request_id="request-a",
        prompt_token_ids=(1,),
        max_new_tokens=1,
    )
    sink = SimpleNamespace(emit=lambda _index, _token: None)
    graph = registry.current_deployment()
    registry.admit(request, sink, pinned_deployment=graph)

    with pytest.raises(DeploymentSelectionError, match="deployment_switch_busy"):
        registry.select("deployment-1")

    registry.release_request("request-a")
    assert registry.request_status("request-a") == "UNKNOWN"
    registry.select("deployment-1")
    assert registry.decode_token(7) == "model-0:7"


def test_registry_rejects_unknown_or_unavailable_deployment() -> None:
    runtimes = [_runtime(0), _runtime(1)]
    registry = LiveDeploymentRegistry(runtimes)
    runtimes[1].route.close()

    with pytest.raises(DeploymentSelectionError, match="deployment_unknown"):
        registry.select("deployment-missing")
    with pytest.raises(DeploymentSelectionError, match="deployment_not_qualified"):
        registry.select("deployment-1")


def test_registry_projects_real_failover_incident_after_selected_route_loss() -> None:
    runtimes = [_runtime(0), _runtime(1)]
    registry = LiveDeploymentRegistry(runtimes)
    runtimes[0].route.close()

    registry.select("deployment-1")

    incident = registry.public_status()["incidents"][-1]
    assert incident == {
        "protocol": "mycelium.live_route_incident.v1",
        "incident_id": "registry-incident-1",
        "deployment_id": "deployment-0",
        "request_id": None,
        "state": "qualified_failover_selected",
        "reason": "route_unavailable",
        "observed_at_unix_ms": incident["observed_at_unix_ms"],
    }


def test_registry_renews_selected_qualification_before_browser_expiry() -> None:
    runtimes = [_runtime(0), _runtime(1)]
    renewed = SimpleNamespace(
        route_ready=True,
        qualification_id="qualification-renewed",
        issued_at_unix_ms=1_000 + LIVE_QUALIFICATION_REFRESH_AFTER_MS,
        deployment_id="deployment-0",
        model_id="Qwen/model-0",
    )
    registry = LiveDeploymentRegistry(
        runtimes,
        qualification_refresher=lambda _runtime: renewed,
        clock_unix_ms=lambda: renewed.issued_at_unix_ms,
    )

    assert registry.current() is renewed
    status = registry.registry_status()["deployments"][0]
    assert status["qualification_id"] == "qualification-renewed"
    assert status["qualified_at_unix_ms"] == renewed.issued_at_unix_ms


def test_registry_defers_renewal_until_active_request_is_released() -> None:
    runtimes = [_runtime(0), _runtime(1)]
    renewed = SimpleNamespace(
        route_ready=True,
        qualification_id="qualification-renewed",
        issued_at_unix_ms=1_000 + LIVE_QUALIFICATION_REFRESH_AFTER_MS,
        deployment_id="deployment-0",
        model_id="Qwen/model-0",
    )
    refreshes = []
    registry = LiveDeploymentRegistry(
        runtimes,
        qualification_refresher=lambda _runtime: refreshes.append("refresh") or renewed,
        clock_unix_ms=lambda: renewed.issued_at_unix_ms,
    )
    assert registry.encode("test") == (1,)
    request = SimpleNamespace(
        request_id="request-renewal-blocker",
        prompt_token_ids=(1,),
        max_new_tokens=1,
    )
    sink = SimpleNamespace(emit=lambda _index, _token: None)
    registry.admit(request, sink, pinned_deployment=registry.current_deployment())

    assert registry.current() is runtimes[0].qualification
    assert refreshes == []

    registry.release_request(request.request_id)
    assert registry.current() is renewed
    assert refreshes == ["refresh"]
