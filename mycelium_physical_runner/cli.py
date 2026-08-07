"""Bounded argv-only command line for the physical runner."""
from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .assembly import build_production_runner
from .config import RunnerConfig, load_operator_plan
from .errors import RunnerError
from .runner import PhysicalRunner
from .state import RunStateDocument, RunnerState, STATE_PROTOCOL
from .state_store import StateStore

RESULT_PROTOCOL = "mycelium.physical_runner_result.v1"
COMMANDS = frozenset(
    {"validate-plan", "live-preflight", "prepare", "diagnose", "qualify", "cancel", "recover", "cleanup"}
)
_STATE_COMMANDS = frozenset({"cancel", "recover", "cleanup"})
RunnerFactory = Callable[[RunnerConfig], PhysicalRunner]


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _error(code: str) -> dict[str, Any]:
    return {"protocol": RESULT_PROTOCOL, "ok": False, "error": {"code": code}}


def _public_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "accepted",
        "blockers",
        "command",
        "deployment_id",
        "hosts",
        "plan_id",
        "preflight_ready",
        "release_ready",
        "route_ready",
        "run_id",
        "state",
        "validated",
    }
    return {key: outcome[key] for key in sorted(allowed & set(outcome))}


def _ok(command: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocol": RESULT_PROTOCOL,
        "ok": True,
        "command": command,
        "outcome": _public_outcome(outcome),
    }


def _parse_arguments(arguments: Sequence[str]) -> tuple[str, str]:
    if not arguments:
        raise RunnerError("cli_arguments_invalid")
    command = arguments[0]
    if command not in COMMANDS:
        raise RunnerError("unsupported_command")
    expected_flag = "--run-state" if command in _STATE_COMMANDS else "--operator-plan"
    if len(arguments) != 3 or arguments[1] != expected_flag or not arguments[2]:
        raise RunnerError("cli_arguments_invalid")
    return command, arguments[2]


def _operator_plan_from_state(path: str) -> str:
    value = StateStore(state_path=path).read()
    if not isinstance(value, dict) or value.get("protocol") != STATE_PROTOCOL:
        raise RunnerError("state_corrupt")
    try:
        document = RunStateDocument(
            plan_id=value["plan_id"],
            run_id=value["run_id"],
            operator_plan_path=value["operator_plan_path"],
            command=value["command"],
            state=RunnerState(value["state"]),
            updated_at_unix_ms=value["updated_at_unix_ms"],
            route_ready=value["route_ready"],
            manifest_digest=value.get("manifest_digest"),
            qualification_id=value.get("qualification_id"),
        )
    except (KeyError, TypeError, ValueError, RunnerError) as exc:
        raise RunnerError("state_corrupt") from exc
    return document.operator_plan_path


def main(
    argv: Sequence[str] | None = None,
    *,
    runner_factory: RunnerFactory | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        command, supplied_path = _parse_arguments(arguments)
        plan_path = _operator_plan_from_state(supplied_path) if command in _STATE_COMMANDS else supplied_path
        if command == "live-preflight":
            try:
                from .live_preflight import load_safe_plan, run_live_preflight
            except ImportError as exc:
                raise RunnerError("live_preflight_unavailable") from exc
            outcome = dict(run_live_preflight(load_safe_plan(plan_path)))
            if outcome.get("route_ready") is not False or outcome.get("release_ready") is not False:
                raise RunnerError("live_preflight_contract_invalid")
            _emit(_ok(command, outcome))
            return 0
        config = load_operator_plan(plan_path)
        if command == "validate-plan":
            _emit(
                _ok(
                    command,
                    {
                        "validated": True,
                        "plan_id": config.plan_id,
                        "run_id": config.run_id,
                        "route_ready": False,
                        "release_ready": False,
                    },
                )
            )
            return 0

        factory = runner_factory or build_production_runner
        outcome = dict(factory(config).execute(command))
        if outcome.get("release_ready") is not False:
            raise RunnerError("runner_result_invalid")
        if command != "qualify" and outcome.get("route_ready") is not False:
            raise RunnerError("runner_result_invalid")
        _emit(_ok(command, outcome))
        return 0
    except RunnerError as exc:
        _emit(_error(exc.code))
        return 2
    except Exception:
        _emit(_error("runner_unexpected"))
        return 2


__all__ = ["COMMANDS", "RESULT_PROTOCOL", "main"]
