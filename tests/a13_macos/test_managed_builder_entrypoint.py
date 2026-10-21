# SPDX-License-Identifier: AGPL-3.0-only
"""Managed builder build()/main() dispatch and plan construction (model-free).

These tests exercise the production entrypoints, not parser helpers: `build()`
runs with its heavyweight stages replaced by inert module-attribute seams (the
same monkeypatch pattern as tests/live/test_qwen_route_builder.py) plus a real
topology document and a real admitted managed-hello observations file. The
seams prove dispatch/threading only: they are NOT model load proof. The
existing SSH/local build behavior is pinned by a characterization test. The
stage-shard emission flag (`--emit-stage-shards`: durable destination,
refuse-if-exists, temporary-directory default unchanged) is pinned on the same
inert seam, including the emitted path it records in `stage_sharding`.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_qwen_live_route as builder


RUN_ID = "macdemo-qwen-20260913T090000Z"
DEPLOYMENT_ID = "5f6f2a46-0a1c-4b8e-9f11-2c2f5aa7b1c3"
_HOST_MATERIAL = hashlib.sha256(b"entrypoint-host-material").hexdigest()[:32]
_BOOT_MATERIAL = hashlib.sha256(b"entrypoint-boot-material").hexdigest()[:32]
_FIXED_NOW = datetime(2026, 9, 13, 9, 0, 30, tzinfo=timezone.utc).timestamp()


@dataclass
class InertDeviceState:
    inert: bool = True


def _managed_node(index: int, *, backend: str = "mlx") -> dict:
    return {
        "node_id": f"node-{index}",
        "process_transport": "managed_https",
        "ssh_target": None,
        "ssh_identity_file": None,
        "staging_root": f"/opt/mycelium/managed/node-{index}",
        "python_executable": "/usr/bin/python3",
        "sidecar_binary": f"/opt/mycelium/managed/node-{index}/sidecar",
        "endpoint_secret_file": f"/opt/mycelium/managed/node-{index}/node.key",
        "endpoint_id": f"endpoint-{index}",
        "membership_generation": index + 1,
        "runtime_backend": backend,
    }


def _observations_document(
    *,
    run_id: str = RUN_ID,
    captured_at_unix_ms: int | None = None,
) -> dict:
    captured = (
        int(_FIXED_NOW * 1_000) if captured_at_unix_ms is None else captured_at_unix_ms
    )
    return {
        "protocol": "mycelium.managed_hello_observations.v1",
        "deployment_id": DEPLOYMENT_ID,
        "run_id": run_id,
        "captured_at_unix_ms": captured,
        "observations": [
            {
                "node_id": f"node-{index}",
                "host_id": (
                    "host-"
                    + hashlib.sha256(
                        (_HOST_MATERIAL + f":node-{index}").encode()
                    ).hexdigest()[:32]
                ),
                "boot_id": (
                    "boot-"
                    + hashlib.sha256(
                        (_BOOT_MATERIAL + f":node-{index}").encode()
                    ).hexdigest()[:32]
                ),
            }
            for index in range(2)
        ],
    }


def _write_observations(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _topology_path(tmp_path: Path, nodes: list[dict]) -> Path:
    document = {"protocol": builder._TOPOLOGY_PROTOCOL, "nodes": nodes}
    path = tmp_path / "topology.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _template() -> dict:
    peers = [
        {
            "node_id": f"node-{index}",
            "ssh_target": None,
            "host_id": f"host-{index}",
            "boot_id": f"boot-{index}",
            "staging_root": f"/opt/mycelium/managed/node-{index}",
            "process_transport": "managed_https",
            "ssh_identity_file": None,
        }
        for index in range(2)
    ]
    runtime_nodes = [
        {
            "node_id": f"node-{index}",
            "python_executable": "/usr/bin/python3",
            "sidecar_binary": f"/opt/mycelium/managed/node-{index}/sidecar",
            "endpoint_secret_file": f"/opt/mycelium/managed/node-{index}/node.key",
        }
        for index in range(2)
    ]
    endpoint_records = [
        {"node_id": f"node-{index}", "endpoint_id": f"endpoint-{index}"}
        for index in range(2)
    ]
    return {
        "controller": {
            "peers": peers,
            "run_plan": {"nodes": runtime_nodes},
            "membership_snapshot": {
                "assignment_offers": [
                    {
                        "message": {
                            "recipient_node_id": f"node-{index}",
                            "generation": index + 1,
                            "peer_endpoint_records": [
                                {
                                    **endpoint_records[1 - index],
                                    "membership_generation": (1 - index) + 1,
                                }
                            ],
                        }
                    }
                    for index in range(2)
                ]
            },
        }
    }


def _inert_prepare(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace every heavyweight stage with inert module-attribute seams.

    Mirrors the monkeypatch pattern of tests/live/test_qwen_route_builder.py;
    proves dispatch only, never model load. ``_copy_runtime_closure`` is
    skipped because its real file-copy behavior is covered separately by
    ``test_runtime_closure_includes_transitive_planner_packages``.
    """

    prepared_manifest = {
        "model_id": builder.MODEL_ID,
        "resolved_commit": builder.MODEL_COMMIT,
        "manifest_digest": {"value": "5" * 64},
        "num_layers": 24,
        "format": "safetensors",
    }
    inert_assignments = [
        {
            "deployment_id": DEPLOYMENT_ID,
            "deployment_epoch": 1,
            "assignment_id": f"assignment-{index}",
            "node_id": f"node-{index}",
            "manifest_digest": "sha256:" + "5" * 64,
            "resolved_commit": builder.MODEL_COMMIT,
            "range": (
                {"start": 0, "end": 12} if index == 0 else {"start": 12, "end": 24}
            ),
            "artifact_cache_root": str(Path("/tmp/inert") / f"node-{index}"),
            "control_plane_binding": None,
            "runtime": {"backend": "mlx"},
        }
        for index in range(2)
    ]

    class Prepared:
        manifest = prepared_manifest
        assignments = inert_assignments
        reports = [{"verified_files": []}, {"verified_files": []}]
        route = object()

    monkeypatch.setattr(
        builder, "prepare_assignment_artifacts", lambda *a, **k: Prepared()
    )
    monkeypatch.setattr(builder, "shard_qwen2_checkpoint", lambda *a, **k: {})
    monkeypatch.setattr(
        builder, "compile_layer_assignments", lambda **k: inert_assignments
    )
    monkeypatch.setattr(
        builder,
        "load_assignment_stage",
        lambda *a, **k: SimpleNamespace(proof={"inert": True}),
    )
    monkeypatch.setattr(
        builder,
        "compile_stage_pack",
        lambda *a, **k: {
            "node_id": a[0]["node_id"],
            "stage_pack_digest": "sha256:" + "6" * 64,
            "artifacts": [],
        },
    )
    monkeypatch.setattr(
        builder,
        "verify_stage_pack",
        lambda *a, **k: {"stage_pack_verification_digest": "sha256:" + "7" * 64},
    )
    monkeypatch.setattr(builder, "artifact_report_for_loader", lambda *a, **k: {})
    monkeypatch.setattr(builder, "provision_assignment", lambda *a, **k: {})
    monkeypatch.setattr(
        builder, "build_execution_graph", lambda *a, **k: SimpleNamespace(stages=[])
    )
    monkeypatch.setattr(
        builder,
        "build_physical_device_states",
        lambda graph: {
            f"node-{index}": InertDeviceState() for index in range(2)
        },
    )
    monkeypatch.setattr(
        builder,
        "Qwen2PromptCodec",
        SimpleNamespace(
            from_deployment=lambda root: SimpleNamespace(
                encode=lambda prompt: (11, 12), decode_token=lambda token: "x"
            )
        ),
    )
    monkeypatch.setattr(builder, "_release_runtime_memory", lambda: None)
    monkeypatch.setattr(
        builder, "execution_graph_to_dict", lambda graph: {"protocol": "graph.v1", "stages": []}
    )
    monkeypatch.setattr(builder, "build_m13_placement_projection", lambda **k: {})
    monkeypatch.setattr(
        builder,
        "AssignmentArtifactCache",
        lambda *a, **k: SimpleNamespace(
            store=lambda *a: None,
            materialize_assignment=lambda **k: {"inert": True},
            status=lambda: {"inert": True},
        ),
    )
    # The node transfer validator requires each node's selection to contain
    # physical_inference_node.py; materialize that one inert file so the real
    # transfer-manifest walk has a valid base.
    def inert_closure(repo, bundle, template):
        bundle.joinpath("physical_inference_node.py").write_text(
            "# inert test fixture\n", encoding="utf-8"
        )

    monkeypatch.setattr(builder, "_copy_runtime_closure", inert_closure)
    monkeypatch.setattr(builder, "artifact_report_errors", lambda *a, **k: [])
    monkeypatch.setattr(
        builder,
        "execute_loaded_stage",
        lambda stage, *, token_ids=None, hidden_states=None: builder.mx.ones(
            (1, len(token_ids), 2), dtype=builder.mx.float32
        )
        if token_ids is not None
        else hidden_states,
    )
    monkeypatch.setattr(builder, "quantized_greedy_token_id", lambda logits: 2)
    return {"assignments": inert_assignments}


def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder.time, "time", lambda: _FIXED_NOW)


def _build_args(tmp_path: Path, topology: Path, observations: Path | None, **extra):
    namespace = dict(
        template_plan=tmp_path / "template.json",
        model_root=tmp_path / "model",
        output_root=tmp_path / "candidate",
        topology=topology,
        route_label="macdemo",
        managed_transport=False,
        managed_hello_observations=observations,
        deployment_id=DEPLOYMENT_ID,
    )
    namespace.update(extra)
    return SimpleNamespace(**namespace)


def test_managed_build_constructs_plan_without_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    observations = _write_observations(
        tmp_path / "observations.json", _observations_document()
    )
    args = _build_args(tmp_path, topology, observations, managed_transport=True)
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("managed build must not spawn any subprocess")

    monkeypatch.setattr(builder.subprocess, "run", explode)

    report = builder.build(args)

    plan = json.loads(Path(report["operator_plan"]).read_text("utf-8"))
    peers = plan["controller"]["peers"]
    assert [peer["process_transport"] for peer in peers] == [
        "managed_https",
        "managed_https",
    ]
    document = _observations_document()
    assert [peer["host_id"] for peer in peers] == [
        observation["host_id"] for observation in document["observations"]
    ]
    assert [peer["boot_id"] for peer in peers] == [
        observation["boot_id"] for observation in document["observations"]
    ]
    # The admitted observations' run_id — which scopes the native identities —
    # is the plan's run_id, not a fresh timestamped one.
    assert plan["run_id"] == RUN_ID
    assert plan["controller"]["run_plan"]["run_id"] == RUN_ID
    assert report["route_ready"] is False
    assert report["model_id"] == builder.MODEL_ID
    assert report["resolved_commit"] == builder.MODEL_COMMIT
    challenge = json.loads(
        (args.output_root / "local-challenge-checkpoint.json").read_text("utf-8")
    )
    assert challenge["state"] == "passed"
    assert challenge["model_id"] == builder.MODEL_ID
    # Startup request pins the CANDIDATE_MODEL seed-0 semantics.
    request = plan["controller"]["run_plan"]["request"]
    assert request["sampling_seed"] == 0
    assert request["request_id"] == "request-macdemo-qwen-startup"


def test_managed_build_fails_closed_when_observations_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    args = _build_args(tmp_path, topology, None, managed_transport=True)
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)
    prepare_calls = {"count": 0}
    real_prepare = builder.prepare_assignment_artifacts

    def tracking_prepare(*a, **k):
        prepare_calls["count"] += 1
        return real_prepare(*a, **k)

    monkeypatch.setattr(builder, "prepare_assignment_artifacts", tracking_prepare)

    with pytest.raises(RuntimeError, match="managed_peer_observations_missing"):
        builder.build(args)

    # Fails closed before any output or expensive preparation.
    assert not args.output_root.exists()
    assert prepare_calls["count"] == 0


def test_managed_build_rejects_stale_observations_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    stale = _write_observations(
        tmp_path / "observations.json",
        _observations_document(
            captured_at_unix_ms=int(_FIXED_NOW * 1_000) - 11 * 60 * 1_000
        ),
    )
    args = _build_args(tmp_path, topology, stale, managed_transport=True)
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)

    with pytest.raises(RuntimeError, match="managed_observation_stale"):
        builder.build(args)

    assert not args.output_root.exists()


def test_ssh_build_still_runs_refresh_without_managed_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Characterization of existing SSH/local behavior: no managed observations
    # required; local peer derives its own identity, remote peer refreshes over
    # SSH exactly as before.
    ssh_nodes = [_managed_node(0), _managed_node(1)]
    ssh_nodes[0]["process_transport"] = "local"
    ssh_nodes[1]["process_transport"] = "ssh"
    ssh_nodes[1]["ssh_target"] = "operator@remote-1"
    ssh_nodes[1]["ssh_identity_file"] = "/keys/remote-1"
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, ssh_nodes)
    args = _build_args(tmp_path, topology, None, managed_transport=False)
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)
    ssh_commands: list[list[str]] = []

    def fake_ssh(command, **_kwargs):
        ssh_commands.append(list(command))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(["host-" + "1" * 32, "boot-" + "2" * 32]),
            stderr="",
        )

    monkeypatch.setattr(builder.subprocess, "run", fake_ssh)
    monkeypatch.setattr(
        builder,
        "derive_local_run_scoped_identity",
        lambda run_id: ("host-" + "3" * 32, "boot-" + "4" * 32),
    )

    report = builder.build(args)

    # Preflight + identity refresh for the one SSH peer only; local node uses
    # its own derivation, never a subprocess.
    assert len(ssh_commands) == 2
    assert all(command[0] == "ssh" for command in ssh_commands)
    peers = json.loads(Path(report["operator_plan"]).read_text("utf-8"))[
        "controller"
    ]["peers"]
    assert [peer["process_transport"] for peer in peers] == ["local", "ssh"]
    assert [peer["host_id"] for peer in peers] == [
        "host-" + "3" * 32,
        "host-" + "1" * 32,
    ]


def test_main_dispatches_managed_build_from_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    observations = _write_observations(
        tmp_path / "observations.json", _observations_document()
    )
    output_root = tmp_path / "candidate"
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)
    captured: dict = {}

    def fake_build(namespace):
        captured["namespace"] = namespace
        return {"operator_plan": str(tmp_path / "plan.json"), "route_ready": False}

    monkeypatch.setattr(builder, "build", fake_build)

    argv = [
        "--template-plan", str(template),
        "--model-root", str(tmp_path / "model"),
        "--output-root", str(output_root),
        "--route-label", "macdemo",
        "--topology", str(topology),
        "--managed-transport",
        "--managed-hello-observations", str(observations),
    ]
    monkeypatch.setattr("sys.argv", ["build_qwen_live_route.py", *argv])

    assert builder.main() == 0

    namespace = captured["namespace"]
    assert namespace.managed_transport is True
    assert Path(namespace.managed_hello_observations) == observations
    # The builder CLI's own default identity stays the historic 0.5B (that
    # module is not part of the wp8-02 G1 composition change); the A13 app
    # passes its selected 7B identity explicitly in the build() namespace
    # (mycelium_a13_macos/chain_compile.py), so this default is inert for the
    # app route.  Left as-is deliberately.
    assert namespace.model_id == builder.MODEL_ID == "Qwen/Qwen2.5-0.5B-Instruct"
    assert (
        namespace.resolved_commit
        == builder.MODEL_COMMIT
        == "7ae557604adf67be50417f59c2c2f167def9a775"
    )
    stdout = capsys.readouterr().out
    assert json.loads(stdout)["route_ready"] is False


def test_main_rejects_managed_observations_without_managed_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    observations = _write_observations(
        tmp_path / "observations.json", _observations_document()
    )
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)
    build_called = {"value": False}
    monkeypatch.setattr(
        builder, "build", lambda namespace: build_called.update(value=True)
    )

    argv = [
        "--template-plan", str(template),
        "--model-root", str(tmp_path / "model"),
        "--output-root", str(tmp_path / "candidate"),
        "--topology", str(topology),
        "--managed-hello-observations", str(observations),
    ]
    monkeypatch.setattr("sys.argv", ["build_qwen_live_route.py", *argv])

    with pytest.raises(
        RuntimeError, match="managed_observations_require_managed_transport"
    ):
        builder.main()
    assert build_called["value"] is False


def test_main_rejects_managed_transport_without_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    observations = _write_observations(
        tmp_path / "observations.json", _observations_document()
    )
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)
    build_called = {"value": False}
    monkeypatch.setattr(
        builder, "build", lambda namespace: build_called.update(value=True)
    )

    argv = [
        "--template-plan", str(template),
        "--model-root", str(tmp_path / "model"),
        "--output-root", str(tmp_path / "candidate"),
        "--managed-transport",
        "--managed-hello-observations", str(observations),
    ]
    monkeypatch.setattr("sys.argv", ["build_qwen_live_route.py", *argv])

    with pytest.raises(RuntimeError, match="managed_transport_requires_topology"):
        builder.main()
    assert build_called["value"] is False


def test_main_rejects_managed_transport_without_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)
    build_called = {"value": False}
    monkeypatch.setattr(
        builder, "build", lambda namespace: build_called.update(value=True)
    )

    argv = [
        "--template-plan", str(template),
        "--model-root", str(tmp_path / "model"),
        "--output-root", str(tmp_path / "candidate"),
        "--topology", str(topology),
        "--managed-transport",
    ]
    monkeypatch.setattr("sys.argv", ["build_qwen_live_route.py", *argv])

    with pytest.raises(RuntimeError, match="managed_peer_observations_missing"):
        builder.main()
    assert build_called["value"] is False


def _record_stage_shard_destination(
    monkeypatch: pytest.MonkeyPatch, captured: dict
) -> None:
    """Record (and materialize) the destination the stage-shard branch selected."""

    def record(source_root, destination_root, **_kwargs):
        destination = Path(destination_root)
        captured["destination"] = destination
        destination.mkdir(parents=True, exist_ok=True)
        return {
            "protocol": "mycelium.stage_sharded_checkpoint.v1",
            "layer_ranges": [
                {"start_layer": 0, "end_layer_exclusive": 12, "layer_count": 12},
                {"start_layer": 12, "end_layer_exclusive": 24, "layer_count": 12},
            ],
        }

    monkeypatch.setattr(builder, "shard_qwen2_checkpoint", record)


def _stage_shard_args(
    tmp_path: Path,
    topology: Path,
    observations: Path,
    **extra,
) -> SimpleNamespace:
    return _build_args(tmp_path, topology, observations, managed_transport=True, **extra)


def test_main_accepts_emit_stage_shards_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    observations = _write_observations(
        tmp_path / "observations.json", _observations_document()
    )
    emit_root = tmp_path / "emitted"
    captured: dict = {}

    def fake_build(namespace):
        captured["namespace"] = namespace
        return {"operator_plan": str(tmp_path / "plan.json"), "route_ready": False}

    monkeypatch.setattr(builder, "build", fake_build)
    base_argv = [
        "--template-plan", str(template),
        "--model-root", str(tmp_path / "model"),
        "--output-root", str(tmp_path / "candidate"),
        "--route-label", "macdemo",
        "--topology", str(topology),
        "--managed-transport",
        "--managed-hello-observations", str(observations),
        "--stage-sharded",
    ]

    monkeypatch.setattr(
        "sys.argv",
        [
            "build_qwen_live_route.py",
            *base_argv,
            "--emit-stage-shards",
            str(emit_root),
        ],
    )
    assert builder.main() == 0
    assert isinstance(captured["namespace"].emit_stage_shards, Path)
    assert captured["namespace"].emit_stage_shards == emit_root

    monkeypatch.setattr("sys.argv", ["build_qwen_live_route.py", *base_argv])
    assert builder.main() == 0
    assert captured["namespace"].emit_stage_shards is None


def test_stage_sharded_emission_uses_durable_root_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    observations = _write_observations(
        tmp_path / "observations.json", _observations_document()
    )
    emit_root = tmp_path / "emitted"
    args = _stage_shard_args(
        tmp_path,
        topology,
        observations,
        stage_sharded=True,
        emit_stage_shards=emit_root,
    )
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)
    captured: dict = {}
    _record_stage_shard_destination(monkeypatch, captured)

    report = builder.build(args)

    destination = captured["destination"]
    assert destination == emit_root / "model"
    assert destination.is_dir()
    assert emit_root.is_dir()
    assert report["stage_sharding"]["emitted_root"] == str(destination)
    checkpoint = json.loads(
        (args.output_root / "local-challenge-checkpoint.json").read_text("utf-8")
    )
    assert checkpoint["stage_sharding"]["emitted_root"] == str(destination)


def test_stage_shards_reuse_emission_writes_durable_repartition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    observations = _write_observations(
        tmp_path / "observations.json", _observations_document()
    )
    reusable_root = tmp_path / "reusable"
    reusable_root.mkdir(mode=0o700)
    emit_root = tmp_path / "emitted"
    args = _stage_shard_args(
        tmp_path,
        topology,
        observations,
        stage_sharded=False,
        reuse_stage_sharded_root=reusable_root,
        emit_stage_shards=emit_root,
    )
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)
    captured: dict = {}
    _record_stage_shard_destination(monkeypatch, captured)

    report = builder.build(args)

    destination = captured["destination"]
    assert destination == emit_root / "model"
    assert destination.is_dir()
    assert report["stage_sharding"]["protocol"] == "mycelium.stage_sharding_reuse.v2"
    assert report["stage_sharding"]["emitted_root"] == str(destination)


def test_emit_stage_shards_refuses_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    observations = _write_observations(
        tmp_path / "observations.json", _observations_document()
    )
    emit_root = tmp_path / "emitted"
    existing = emit_root / "model"
    existing.mkdir(parents=True)
    sentinel = existing / "prior-emission.safetensors"
    sentinel.write_bytes(b"prior emission")
    args = _stage_shard_args(
        tmp_path,
        topology,
        observations,
        stage_sharded=True,
        emit_stage_shards=emit_root,
    )
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)
    calls = {"count": 0}

    def fail_loud(*_args: object, **_kwargs: object) -> dict:
        calls["count"] += 1
        raise AssertionError("stage sharding must not run over an existing emission")

    monkeypatch.setattr(builder, "shard_qwen2_checkpoint", fail_loud)

    with pytest.raises(RuntimeError, match="^emit_stage_shards_already_exists$"):
        builder.build(args)

    assert calls["count"] == 0
    assert sentinel.read_bytes() == b"prior emission"


@pytest.mark.parametrize("stage_sharded", [True, False])
def test_stage_shards_default_still_uses_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage_sharded: bool
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    observations = _write_observations(
        tmp_path / "observations.json", _observations_document()
    )
    extra: dict = {"stage_sharded": stage_sharded}
    if not stage_sharded:
        reusable_root = tmp_path / "reusable"
        reusable_root.mkdir(mode=0o700)
        extra["reuse_stage_sharded_root"] = reusable_root
    args = _stage_shard_args(tmp_path, topology, observations, **extra)
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)
    captured: dict = {}
    _record_stage_shard_destination(monkeypatch, captured)

    report = builder.build(args)

    destination = captured["destination"]
    assert destination.name == "model"
    assert "stage-shards" in destination.parent.name
    assert not destination.exists()
    assert "emitted_root" not in report["stage_sharding"]


def test_emit_stage_shards_requires_a_stage_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    observations = _write_observations(
        tmp_path / "observations.json", _observations_document()
    )
    emit_root = tmp_path / "emitted"
    args = _stage_shard_args(
        tmp_path,
        topology,
        observations,
        emit_stage_shards=emit_root,
    )
    _inert_prepare(monkeypatch)
    _fixed_clock(monkeypatch)

    with pytest.raises(RuntimeError, match="^emit_stage_shards_requires_stage_branch$"):
        builder.build(args)
    assert not emit_root.exists()
