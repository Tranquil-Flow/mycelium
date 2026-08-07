from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mycelium_physical_runner.cli import main
from mycelium_physical_runner.state import RunStateDocument, RunnerState
from mycelium_physical_runner.state_store import StateStore
from tests.physical_runner.conftest import operator_plan_payload, write_operator_plan


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.state = "cleanup-complete"

    def execute(self, command: str) -> dict[str, Any]:
        self.commands.append(command)
        return {
            "command": command,
            "accepted": command == "qualify",
            "route_ready": command == "qualify",
            "release_ready": False,
        }


def _invoke(tmp_path: Path, capsys: Any, command: str, *, runner: FakeRunner | None = None) -> tuple[int, dict[str, Any]]:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    plan = write_operator_plan(tmp_path / "plan.json", operator_plan_payload(workspace))
    code = main(
        [command, "--operator-plan", str(plan)],
        runner_factory=(lambda _config: runner) if runner is not None else None,
    )
    line = capsys.readouterr().out
    return code, json.loads(line)


def test_validate_plan_is_real_bounded_validation_and_never_claims_readiness(tmp_path: Path, capsys: Any) -> None:
    code, envelope = _invoke(tmp_path, capsys, "validate-plan")
    assert code == 0
    assert envelope["ok"] is True
    assert envelope["outcome"]["validated"] is True
    assert envelope["outcome"]["route_ready"] is False
    assert envelope["outcome"]["release_ready"] is False


def test_positional_plan_syntax_is_rejected_without_echoing_private_path(tmp_path: Path, capsys: Any) -> None:
    plan = tmp_path / "private-plan.json"
    code = main(["validate-plan", str(plan)])
    envelope = json.loads(capsys.readouterr().out)
    assert code != 0
    assert envelope["error"]["code"] == "cli_arguments_invalid"
    assert str(plan) not in json.dumps(envelope)


def test_physical_command_is_dispatched_once_through_runner_factory(tmp_path: Path, capsys: Any) -> None:
    runner = FakeRunner()
    code, envelope = _invoke(tmp_path, capsys, "qualify", runner=runner)
    assert code == 0
    assert runner.commands == ["qualify"]
    assert envelope["outcome"]["route_ready"] is True
    assert envelope["outcome"]["release_ready"] is False


def test_unsupported_command_is_nonzero_and_does_not_echo_plan(tmp_path: Path, capsys: Any) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    plan = write_operator_plan(tmp_path / "plan.json", operator_plan_payload(workspace))
    code = main(["not-a-command", "--operator-plan", str(plan)])
    envelope = json.loads(capsys.readouterr().out)
    assert code != 0
    assert envelope["error"]["code"] == "unsupported_command"
    assert str(plan) not in json.dumps(envelope)


def test_run_state_commands_require_run_state_flag(tmp_path: Path, capsys: Any) -> None:
    code = main(["cleanup", "--operator-plan", str(tmp_path / "plan.json")])
    envelope = json.loads(capsys.readouterr().out)
    assert code != 0
    assert envelope["error"]["code"] == "cli_arguments_invalid"


def test_live_preflight_loads_safe_plan_and_uses_production_bridge(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    safe_plan = {
        "protocol": "mycelium.physical_runner_safe_plan.v1",
        "run_id": "run-w8-001",
        "deployment_id": "deployment-w8-001",
        "route_ready": False,
        "release_ready": False,
        "hosts": [],
    }
    path = tmp_path / "safe-plan.json"
    path.write_text(json.dumps(safe_plan), encoding="utf-8")
    observed: list[dict[str, Any]] = []

    def bridge(value: dict[str, Any]) -> dict[str, Any]:
        observed.append(value)
        return {
            "protocol": "mycelium.physical_runner_live_preflight.v1",
            "run_id": value["run_id"],
            "deployment_id": value["deployment_id"],
            "preflight_ready": False,
            "route_ready": False,
            "release_ready": False,
            "blockers": [{"code": "physical_probe_pending"}],
            "hosts": [],
        }

    monkeypatch.setattr("mycelium_physical_runner.live_preflight.run_live_preflight", bridge)
    code = main(["live-preflight", "--operator-plan", str(path)])
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0
    assert observed == [safe_plan]
    assert envelope["outcome"]["route_ready"] is False
    assert envelope["outcome"]["release_ready"] is False


def test_cleanup_resolves_operator_plan_only_through_run_state(
    tmp_path: Path,
    capsys: Any,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    plan = write_operator_plan(tmp_path / "plan.json", operator_plan_payload(workspace))
    state_path = tmp_path / "private" / "state.json"
    StateStore(state_path=state_path).write(
        RunStateDocument(
            plan_id="plan-test",
            run_id="run-test",
            operator_plan_path=str(plan),
            command="prepare",
            state=RunnerState.UNREADY,
            updated_at_unix_ms=1,
            route_ready=False,
        )
    )
    runner = FakeRunner()
    code = main(
        ["cleanup", "--run-state", str(state_path)],
        runner_factory=lambda _config: runner,
    )
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0
    assert runner.commands == ["cleanup"]
    assert envelope["outcome"]["route_ready"] is False
