# SPDX-License-Identifier: AGPL-3.0-only
"""A7 route authority installation is gated by the nodes' attested A7 configs.

Each node attests its validated ``a7/node.json`` in its signed ``configured``
observation; the supervisor derives A7 staging from the route's verified
observations, never from node-0's disk (a remote source's staging root is not
readable there).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from unittest.mock import Mock, patch

from mycelium_a7_node_wiring import A7_NODE_CONFIG_PROTOCOL
from mycelium_live.supervisor import ColdRouteRestartOwner
from mycelium_live.a7_authority import install_route_authority


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _placement(placement_id: str, node_id: str, lifecycle_state: str) -> Any:
    return SimpleNamespace(
        placement_id=placement_id,
        node_id=node_id,
        lifecycle_state=lifecycle_state,
        runtime_backend="mlx",
    )


def _graph() -> Any:
    return SimpleNamespace(
        deployment_id="deployment-a7",
        deployment_epoch=1,
        topology_version=1,
        model_id="model-a7",
        resolved_commit="commit-a7",
        manifest_digest=_digest("m"),
        stages=(
            SimpleNamespace(
                stage_id="0",
                placements=(
                    _placement("entry-p0", "node-0", "ACTIVE"),
                ),
            ),
            SimpleNamespace(
                stage_id="1",
                placements=(
                    _placement("source-p1", "node-2", "ACTIVE"),
                    _placement("standby-p1", "node-0", "STANDBY"),
                ),
            ),
        ),
    )


class _Route:
    def __init__(self, root: Path) -> None:
        self.execution_graph = _graph()
        # node-2's staging root is on the laptop: it does not exist on this host.
        self._peers = {
            "node-0": SimpleNamespace(node_id="node-0", staging_root=str(root / "node-0")),
            "node-2": SimpleNamespace(node_id="node-2", staging_root="/Users/member/mycelium-a7-n2"),
        }
        self._signed_observations: list[dict[str, Any]] = []
        self._peer_exit_successor_authority: Callable[..., bool] | None = None
        self.public_status = Mock(name="public_status")

    def set_peer_exit_successor_authority(self, authority: Callable[..., bool] | None) -> None:
        self._peer_exit_successor_authority = authority


class _RouterPortWrapper:
    """Minimal LiveRouterPort-shaped wrapper; install must land on its route."""

    def __init__(self, route: _Route) -> None:
        self._route = route

    def current_deployment(self) -> Any:
        return self._route.execution_graph


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _entry_config() -> dict[str, Any]:
    incarnation = _digest("i")
    return {
        "protocol": A7_NODE_CONFIG_PROTOCOL,
        "deployment_id": "deployment-a7",
        "node_id": "node-0",
        "standby": {
            "placement_id": "standby-p1",
            "placement_digest": _digest("s"),
            "incarnation": incarnation,
            "assignment_file": "standby/assignment.json",
            "manifest_file": "standby/manifest.json",
            "stage_pack_file": "standby/stage-pack.json",
            "load_generation": 1,
            "secret_file": "a7/standby.secret",
            "max_checkpoint_bytes": 1024,
            "max_retained_checkpoints": 4,
        },
        "source": None,
        "entry": {
            "fence_directory": "a7/fences",
            "coverage": [
                {
                    "stage_id": "1",
                    "source_placement_id": "source-p1",
                    "source_node_id": "node-2",
                    "standby_placement_id": "standby-p1",
                    "standby_node_id": "node-0",
                    "standby_incarnation": incarnation,
                    "standby_signer_record": {},
                    "qualification_file": "a7/standby-qualification.json",
                }
            ],
            "a6_available": True,
            "ack_timeout_seconds": 1.0,
            "receipt_timeout_seconds": 1.0,
        },
    }


def _source_config() -> dict[str, Any]:
    incarnation = _digest("i")
    return {
        "protocol": A7_NODE_CONFIG_PROTOCOL,
        "deployment_id": "deployment-a7",
        "node_id": "node-2",
        "standby": None,
        "source": {
            "source_placement_id": "source-p1",
            "source_placement_digest": _digest("q"),
            "stage_id": "1",
            "standby_node_id": "node-0",
            "standby_placement_id": "standby-p1",
            "standby_placement_digest": _digest("s"),
            "standby_incarnation": incarnation,
            "standby_signer_record": {},
            "standby_key_record": {},
            "max_checkpoint_bytes": 1024,
        },
        "entry": None,
    }


def _configured(node_id: str, a7_config: Mapping[str, Any] | None) -> dict[str, Any]:
    details: dict[str, Any] = {"assignment_id": f"assignment-{node_id}", "runtime_mode": "stage_local_kv"}
    if a7_config is not None:
        details["a7_node_config"] = dict(a7_config)
    return {"observation": {"node_id": node_id, "event": "configured", "details": details}}


def _attest_a7(route: _Route, *, entry: Mapping[str, Any] | None = None, source: Mapping[str, Any] | None = None) -> None:
    route._signed_observations.append(_configured("node-0", _entry_config() if entry is None else entry))
    route._signed_observations.append(_configured("node-2", _source_config() if source is None else source))


def _stage_a7(root: Path, route: _Route) -> None:
    _attest_a7(route)


def _owner() -> ColdRouteRestartOwner:
    return ColdRouteRestartOwner(route_factory=lambda: None, open_and_qualify=lambda _route, _generation: None)  # type: ignore[arg-type]


def test_apply_cached_configuration_installs_authority_only_when_a7_is_staged(tmp_path: Path) -> None:
    route = _Route(tmp_path)
    _stage_a7(tmp_path, route)

    _owner()._apply_cached_configuration(route)

    assert route._peer_exit_successor_authority is not None
    route.public_status.assert_not_called()


def test_apply_cached_configuration_leaves_unstaged_route_without_authority(tmp_path: Path) -> None:
    route = _Route(tmp_path)

    _owner()._apply_cached_configuration(route)

    assert route._peer_exit_successor_authority is None
    route.public_status.assert_not_called()


def test_apply_cached_configuration_sets_the_inner_live_route_when_wrapped(tmp_path: Path) -> None:
    route = _Route(tmp_path)
    wrapper = _RouterPortWrapper(route)
    _stage_a7(tmp_path, route)

    _owner()._apply_cached_configuration(wrapper)  # type: ignore[arg-type]

    assert route._peer_exit_successor_authority is not None


def test_disk_staged_configs_without_attestation_install_nothing(tmp_path: Path) -> None:
    route = _Route(tmp_path)
    _write_json(tmp_path / "node-0" / "a7" / "node.json", _entry_config())
    route._signed_observations += [_configured("node-0", None), _configured("node-2", None)]

    _owner()._apply_cached_configuration(route)

    assert route._peer_exit_successor_authority is None


def test_only_entry_attested_without_the_remote_source_installs_nothing(tmp_path: Path) -> None:
    route = _Route(tmp_path)
    route._signed_observations += [_configured("node-0", _entry_config()), _configured("node-2", None)]

    _owner()._apply_cached_configuration(route)

    assert route._peer_exit_successor_authority is None


def test_attestation_mismatch_fails_closed(tmp_path: Path) -> None:
    # A config attested by a node for another node id.
    route = _Route(tmp_path)
    _attest_a7(route, source={**_source_config(), "node_id": "node-0"})
    _owner()._apply_cached_configuration(route)
    assert route._peer_exit_successor_authority is None
    # A coverage naming a placement the source did not attest.
    route = _Route(tmp_path)
    entry = _entry_config()
    entry["entry"]["coverage"][0]["source_placement_id"] = "entry-p0"
    _attest_a7(route, entry=entry)
    _owner()._apply_cached_configuration(route)
    assert route._peer_exit_successor_authority is None
    # An attestation from a node that is not a route peer.
    route = _Route(tmp_path)
    _attest_a7(route)
    route._signed_observations.append(_configured("node-9", _source_config()))
    _owner()._apply_cached_configuration(route)
    assert route._peer_exit_successor_authority is None


def test_latest_configured_observation_per_node_wins(tmp_path: Path) -> None:
    route = _Route(tmp_path)
    _attest_a7(route)
    route._signed_observations.append(_configured("node-2", None))  # the source restarted without A7

    _owner()._apply_cached_configuration(route)

    assert route._peer_exit_successor_authority is None


def _install_for_closure() -> tuple[_Route, Callable[..., bool]]:
    route = _Route(Path("/tmp/a7-authority-test"))
    install_route_authority(
        route,
        node_id_for_command="node-0",
        accept_source_lost=lambda _request_id, _lost_node_id: {"accepted": True},
        cut_over_lost_source=lambda _router, _request_id, _lost_node_id: True,
    )
    assert route._peer_exit_successor_authority is not None
    return route, route._peer_exit_successor_authority


def test_authority_closure_sends_a7_source_lost_once_with_a_one_second_deadline() -> None:
    route, authority = _install_for_closure()
    control = {"path_id": "path-a7", "path_attempt": 1, "publisher_generation": 2}
    calls: list[tuple[str, dict[str, Any], float, float]] = []

    def send_to_entry(command: str, payload: Mapping[str, Any], deadline: float) -> dict[str, Any]:
        called_at = time.monotonic()
        calls.append((command, dict(payload), deadline, called_at))
        return {
            "event": "a7_source_lost",
            "details": {
                "accepted": True,
                "request_id": "request-a7",
                "lost_node_id": "node-2",
                "control": dict(control),
            },
        }

    with patch("threading.Lock") as lock_factory:
        accepted = authority(
            request_id="request-a7",
            lost_node_id="node-2",
            control=control,
            send_to_entry=send_to_entry,
        )

    assert accepted is True
    assert len(calls) == 1
    command, payload, deadline, called_at = calls[0]
    assert command == "a7_source_lost"
    assert payload == {
        "request_id": "request-a7",
        "lost_node_id": "node-2",
        "control": control,
    }
    assert 0.0 < deadline - called_at <= 1.0
    route.public_status.assert_not_called()
    lock_factory.assert_not_called()


def test_authority_closure_refuses_unsigned_or_mismatched_details() -> None:
    _route, authority = _install_for_closure()
    control = {"path_id": "path-a7", "path_attempt": 1}

    def result(details: Mapping[str, Any]) -> bool:
        calls: list[str] = []

        def send_to_entry(command: str, payload: Mapping[str, Any], deadline: float) -> dict[str, Any]:
            calls.append(command)
            return {"event": "a7_source_lost", "details": dict(details)}

        accepted = authority(
            request_id="request-a7",
            lost_node_id="node-2",
            control=control,
            send_to_entry=send_to_entry,
        )
        assert calls == ["a7_source_lost"]
        return accepted

    assert result({"accepted": False, "request_id": "request-a7", "lost_node_id": "node-2", "control": control}) is False
    assert result({"accepted": True, "request_id": "other", "lost_node_id": "node-2", "control": control}) is False
    assert result({"accepted": True, "request_id": "request-a7", "lost_node_id": "other", "control": control}) is False
    assert result({"accepted": True, "request_id": "request-a7", "lost_node_id": "node-2", "control": {"path_id": "other"}}) is False
    assert result({"accepted": True, "request_id": "request-a7", "lost_node_id": "node-2"}) is False
