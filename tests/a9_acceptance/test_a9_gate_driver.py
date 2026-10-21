# SPDX-License-Identifier: AGPL-3.0-only
"""End-to-end rehearsal of scripts/run_a9_gate.py on the authoring host.

A real seed database with two durable members, real node key files and
durable membership state, the seed-export -> member-sign -> interrupted and
restarted migrate -> negatives -> summary sequence, and the Windows probe and
stage benchmark in conformance-host mode over a tiny Qwen2 snapshot.  Nothing
here is physical or Windows evidence.
"""

from __future__ import annotations

from itertools import count
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import numpy as np
import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_node.durable_membership import save_membership_state
from mycelium_seed import SeedCoordinator, SqliteSeedState
from tests.host_platform.portable import make_private_directory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import run_a9_gate as gate_driver  # noqa: E402

BUILD = "sha256:" + "b" * 64
CAPABILITY = {"runtime_backend": "mlx", "transport": "iroh", "activation_protocol": "mycelium.router_wire.v1"}
REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"


def _ids(prefix):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


@pytest.fixture()
def swarm(tmp_path):
    import time

    now = time.time()
    seed_home = tmp_path / "seed"
    make_private_directory(seed_home)
    seed_signer = load_or_create_node_signer(seed_home / "identity" / "seed.key")
    database = seed_home / "state" / "state.sqlite3"
    coordinator = SeedCoordinator(
        swarm_id="swarm-a", seed_node_id="seed-node", seed_url="http://127.0.0.1:8788",
        signer=seed_signer, invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database), incarnation="seed-incarnation",
        clock=lambda: now, id_source=_ids("seed"), lease_seconds=900.0,
    )
    data_dirs = {}
    for index, node_id in enumerate(("node-a", "node-b")):
        data_dir = tmp_path / node_id
        make_private_directory(data_dir)
        signer = load_or_create_node_signer(data_dir / "identity" / "node.key")
        session = NodeMembershipSession(
            node_id=node_id, swarm_id="swarm-a", seed_node_id="seed-node", signer=signer,
            incarnation="incarnation-a", software_version="mycelium-test", peer_class="mac_mlx_iroh",
            runtime_capability=CAPABILITY, clock=lambda: now, id_source=_ids(node_id),
        )
        bundle = coordinator.mint_invite(nonce=f"invite-{index}", ttl_seconds=120)
        verified = verify_invite_bundle(bundle, now=now)
        request = session.join_request(invite_nonce=verified["payload"]["nonce"],
                                       endpoint_addrs=["https://203.0.113.11:9443/control"])
        session.accept_join(coordinator.accept_join(invite_token=bundle["token"], join_envelope=request),
                            seed_key_digest=verified["seed_key_digest"])
        save_membership_state(data_dir, {
            "protocol": "mycelium.node.membership_state.v1", "node_id": node_id, "swarm_id": "swarm-a",
            "seed_node_id": "seed-node", "seed_url": "http://127.0.0.1:8788",
            "seed_key_digest": seed_signer.verification_key_digest,
            "seed_key_records": [seed_signer.public_key_record()],
            "endpoint_id": signer.endpoint_id, "incarnation": "incarnation-a",
            "membership_generation": session.generation, "restart_count": 0,
        })
        data_dirs[node_id] = data_dir
    return seed_home, database, data_dirs


def _run(*argv):
    return gate_driver.main([str(item) for item in argv])


@pytest.mark.skipif(sys.platform == "win32", reason="macOS mlx members: member-sign observes this host and correctly refuses a macOS class on Windows")
def test_migration_sequence_writes_receipts_and_refuses_after_cutover(swarm, tmp_path, capsys):
    seed_home, database, data_dirs = swarm
    gate = tmp_path / "gate"
    assert _run("seed-export", "--gate-dir", gate, "--seed-db", database, "--seed-key",
                seed_home / "identity" / "seed.key", "--swarm-id", "swarm-a",
                "--build-digest", BUILD, "--software-version", "1.0") == 0
    export = json.loads((gate / "01-seed-export.json").read_text())
    assert export["member_count"] == 2 and export["seconds_remaining"] > 0
    profiles = tmp_path / "profiles"
    for node_id, data_dir in data_dirs.items():
        assert _run("member-sign", "--request", gate / "requests" / f"{node_id}.json", "--data-dir", data_dir,
                    "--new-incarnation", "incarnation-v2", "--out", profiles / f"{node_id}.json") == 0
    # A request addressed to another member is refused on this member host.
    with pytest.raises(ValueError, match="not_for_this_member"):
        _run("member-sign", "--request", gate / "requests" / "node-b.json", "--data-dir", data_dirs["node-a"],
             "--new-incarnation", "incarnation-v2", "--out", tmp_path / "wrong.json")
    assert _run("migrate", "--gate-dir", gate, "--profiles", profiles, "--stop-after", "commit_pending") == 0
    assert (gate / "02-interrupted-commit_pending.json").exists()
    assert _run("migrate", "--gate-dir", gate, "--profiles", profiles) == 0
    receipt = json.loads((gate / "03-migration.json").read_text())
    assert receipt["restart"]["checkpoint_before"] == "commit_pending"
    assert receipt["status_projection"]["status"] == "complete"
    assert receipt["status_projection"]["member_count"] == 2
    assert all(set(roles.values()) == {"ineligible"} for roles in receipt["roles_after_migration"].values())
    public = (gate / "03-migration.json").read_text()
    assert "verification_key" not in public and "incarnation-v2" not in public
    capsys.readouterr()
    assert _run("negatives", "--gate-dir", gate, "--profiles", profiles, "--build-digest", BUILD) == 0
    negatives = json.loads((gate / "04-negatives.json").read_text())
    assert negatives["all_passed"] is True
    assert negatives["results"]["changed_key_migration"]["prior_database_preserved"] is True
    assert _run("summary", "--gate-dir", gate) == 0
    summary = json.loads((gate / "99-summary.json").read_text())
    assert summary["status"]["migration + cutover + reader removal"] is True
    assert summary["status"]["windows stage benchmark"] is False


def _tiny_snapshot(base: Path) -> Path:
    snapshot = base / "snapshots" / REVISION
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text(json.dumps({
        "model_type": "qwen2", "hidden_size": 32, "num_attention_heads": 4, "num_hidden_layers": 2,
        "num_key_value_heads": 2, "intermediate_size": 64, "vocab_size": 96,
        "max_position_embeddings": 256, "rms_norm_eps": 1e-6, "rope_theta": 1e6, "tie_word_embeddings": True,
    }))
    rng = np.random.default_rng(3)
    shapes = {"model.embed_tokens.weight": (96, 32), "model.norm.weight": (32,)}
    per_layer = {
        "input_layernorm.weight": (32,), "self_attn.q_proj.weight": (32, 32), "self_attn.q_proj.bias": (32,),
        "self_attn.k_proj.weight": (16, 32), "self_attn.k_proj.bias": (16,),
        "self_attn.v_proj.weight": (16, 32), "self_attn.v_proj.bias": (16,),
        "self_attn.o_proj.weight": (32, 32), "post_attention_layernorm.weight": (32,),
        "mlp.gate_proj.weight": (64, 32), "mlp.up_proj.weight": (64, 32), "mlp.down_proj.weight": (32, 64),
    }
    for layer in range(2):
        shapes.update({f"model.layers.{layer}.{key}": shape for key, shape in per_layer.items()})
    header, payload, offset = {}, b"", 0
    for key, shape in shapes.items():
        centre = 1.0 if key.endswith("norm.weight") else 0.0
        data = rng.normal(centre, 0.05, shape).astype("<f4").tobytes()
        header[key] = {"dtype": "F32", "shape": list(shape), "data_offsets": [offset, offset + len(data)]}
        payload += data
        offset += len(data)
    encoded = json.dumps(header).encode()
    (snapshot / "model.safetensors").write_bytes(struct.pack("<Q", len(encoded)) + encoded + payload)
    return snapshot


@pytest.fixture(scope="module")
def kernel(tmp_path_factory):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("no C compiler")
    target = tmp_path_factory.mktemp("k") / "mycelium_int8.so"
    subprocess.run([compiler, "-O2", "-shared", "-fPIC", "-o", str(target),
                    str(ROOT / "native/a9_int8_kernel/mycelium_int8.c")], check=True)
    from mycelium_platform_neutral.native_int8 import file_digest

    return target, file_digest(target)


def test_windows_commands_refuse_non_windows_hosts_unless_conformance(kernel, tmp_path, capsys):
    library, digest = kernel
    gate = tmp_path / "gate"
    assert _run("windows-probe", "--gate-dir", gate, "--kernel", library, "--kernel-digest", digest) == 2
    assert _run("windows-probe", "--gate-dir", gate, "--kernel", library, "--kernel-digest", digest,
                "--threads", "1", "--conformance-host") == 0
    probe = json.loads((gate / "10-windows-probe.json").read_text())
    assert probe["passed"] is True and probe["conformance_host"] is True
    snapshot = _tiny_snapshot(tmp_path / "model")
    capsys.readouterr()
    assert _run("windows-benchmark", "--gate-dir", gate, "--kernel", library, "--kernel-digest", digest,
                "--threads", "1", "--conformance-host", "--snapshot", snapshot,
                "--profile-digest", "local-declaration", "--thermal-state", "nominal",
                "--prompt-lengths", "4", "9", "--output-lengths", "3", "5", "--repeats-per-cell", "2") == 0
    receipt = json.loads((gate / "12-windows-benchmark.json").read_text())
    decision = receipt["efficiency_qualification"]["decision"]
    assert decision["state"] == "not_performance_ready"
    assert "portable_backend_cannot_be_performance_ready" in decision["blockers"]
    assert "short_output_insufficient_tokens" in decision["blockers"]
    assert receipt["result"]["token_parity_mismatches"] == []
    assert receipt["windows_host"] is False
    assert receipt["profile_binding"] == "unsigned_local_declaration"
    assert receipt["thermal"]["source_kind"] == "operator_declared"
    assert receipt["thermal"]["native_unavailable"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="macOS mlx members: member-sign observes this host and correctly refuses a macOS class on Windows")
def test_live_cutover_adoption_and_v2_resume_through_the_driver(swarm, tmp_path, capsys):
    import time

    from mycelium_node.durable_membership import load_membership_state, load_v2_profile_template
    from mycelium_node.__main__ import _v2_peer_profile
    from mycelium_qualification.signing import generate_ed25519_signer

    seed_home, database, data_dirs = swarm
    gate = tmp_path / "gate"
    seed_key = seed_home / "identity" / "seed.key"
    assert _run("seed-export", "--gate-dir", gate, "--seed-db", database, "--seed-key", seed_key,
                "--swarm-id", "swarm-a", "--build-digest", BUILD, "--software-version", "1.0") == 0
    profiles = tmp_path / "profiles"
    for node_id, data_dir in data_dirs.items():
        assert _run("member-sign", "--request", gate / "requests" / f"{node_id}.json", "--data-dir", data_dir,
                    "--new-incarnation", "incarnation-v2", "--out", profiles / f"{node_id}.json") == 0
    assert _run("migrate", "--gate-dir", gate, "--profiles", profiles) == 0
    assert _run("seed-commit", "--gate-dir", gate, "--seed-db", database, "--seed-key", seed_key,
                "--swarm-id", "swarm-a") == 2  # refuses without --seed-stopped
    assert _run("seed-commit", "--gate-dir", gate, "--seed-db", database, "--seed-key", seed_key,
                "--swarm-id", "swarm-a", "--seed-stopped") == 0
    assert SqliteSeedState(database).membership_contract()["version"] == "v2"
    assert (gate / "seed-incumbent" / "state-before-cutover.sqlite3").exists()
    # A foreign adoption (node-b's) is refused on node-a.
    with pytest.raises(ValueError, match="not_for_this_member"):
        _run("member-adopt", "--adoption", gate / "adoptions" / "node-b.json",
             "--profile", profiles / "node-a.json", "--data-dir", data_dirs["node-a"])
    for node_id, data_dir in data_dirs.items():
        assert _run("member-adopt", "--adoption", gate / "adoptions" / f"{node_id}.json",
                    "--profile", profiles / f"{node_id}.json", "--data-dir", data_dir) == 0
    state = load_membership_state(data_dirs["node-a"])
    assert (state["membership_generation"], state["incarnation"]) == (2, "incarnation-v2")
    # The restarted node resumes with its re-signed v2 declaration.
    now = time.time()
    seed_signer = load_or_create_node_signer(seed_key)
    coordinator = SeedCoordinator(
        swarm_id="swarm-a", seed_node_id="seed-node", seed_url="http://127.0.0.1:8788",
        signer=seed_signer, invite_registry=SqliteInviteRegistry(database), state=SqliteSeedState(database),
        incarnation="seed-incarnation-2", clock=lambda: now, id_source=_ids("restarted"), lease_seconds=900.0)
    node_signer = load_or_create_node_signer(data_dirs["node-a"] / "identity" / "node.key")
    session = NodeMembershipSession(
        node_id="node-a", swarm_id="swarm-a", seed_node_id="seed-node", signer=node_signer,
        incarnation="incarnation-v3", software_version="mycelium-test", peer_class="mac_mlx_iroh",
        runtime_capability=CAPABILITY, clock=lambda: now, id_source=_ids("node-a-v3"))
    profile = _v2_peer_profile(load_v2_profile_template(data_dirs["node-a"]), signer=node_signer,
                               incarnation="incarnation-v3", membership_generation=3)
    request = session.resume_request(previous_generation=2, previous_incarnation="incarnation-v2",
                                     endpoint_addrs=["https://203.0.113.11:9443/control"], peer_profile=profile)
    session.accept_resume(coordinator.accept_resume(resume_envelope=request),
                          seed_key_digest=seed_signer.verification_key_digest)
    assert session.generation == 3
    # A post-cutover newcomer declares a v2 profile and has no v1 history.
    newcomer = tmp_path / "node-new"
    assert _run("member-declare", "--data-dir", newcomer, "--node-id", "node-new", "--swarm-id", "swarm-a",
                "--software-version", "1.0", "--build-digest", BUILD, "--runtime", "mlx") == 0
    assert load_v2_profile_template(newcomer)["profile"]["runtimes"][0]["backend_id"] == "mlx"
    del generate_ed25519_signer
