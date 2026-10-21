# SPDX-License-Identifier: AGPL-3.0-only
"""A release-path node acquires and applies a v2 Qwen2 stage end to end.

A real seed (HTTP member channel), the product ``NodeCore`` (via
``native/mobile_core/examples/node_cli``, the code path the Android app runs)
and an A2-over-iroh artifact source (``examples/artifact_source``) on
loopback: the owner issues a seed-signed v2 manifest naming the source; the
node receives the assignment with its seed grant, fetches every pinned object
from the source (receipt-verified, digest-verified, atomically promoted),
loads the stage, passes its host golden and only then applies the assignment.
A withdrawal unloads it, and a pause reading keeps the stage unapplied.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import threading
import queue

import pytest

from mycelium_mobile import qwen_stage_manifest as qm
from mycelium_seed import route_assignment as ra

from .test_node_lease_recovery import CORE_CRATE, Node, Seed, _BUILD_ENVIRONMENT_KEYS, _held, node_cli  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "native" / "mobile_core" / "tests" / "fixtures"
V2 = json.loads((FIXTURES / "qwen2_stage_manifest_v2.json").read_text())
PEER = "0d7550754e0800a5d237eef5826035766b9b3e5a15868a940ab289958788e3b0"
SOURCE_BIN = CORE_CRATE / "target" / "debug" / "examples" / "artifact_source"
COOL = ("--platform-sample", "battery_level_percent=80,charging=1,battery_temperature_celsius=30")
HOT = ("--platform-sample", "battery_level_percent=80,charging=1,battery_temperature_celsius=60")


@pytest.fixture(scope="module")
def source_bin(node_cli: Path) -> Path:  # noqa: F811 - builds node_cli first
    environment = {key: os.environ[key] for key in _BUILD_ENVIRONMENT_KEYS if key in os.environ}
    environment["CARGO_NET_OFFLINE"] = "true"
    subprocess.run(
        ["cargo", "build", "--locked", "--features", "net", "--example", "artifact_source"],
        cwd=CORE_CRATE, env=environment, check=True,
    )
    assert SOURCE_BIN.is_file()
    return SOURCE_BIN


@pytest.fixture
def seed(tmp_path: Path):
    seed = Seed(tmp_path / "seed")
    yield seed
    seed.close()


class Source:
    """The artifact source process holding the synthetic stage's objects."""

    def __init__(self, binary: Path, tmp: Path, seed_key: dict) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        objects = tmp / "objects"
        model = tmp / "model.safetensors"
        model.write_bytes(base64.b64decode(json.loads((FIXTURES / "qwen2_synthetic.json").read_text())["safetensors_b64"]))
        qm.export_tensor_objects(model, V2["manifest"], objects)
        (tmp / "seed-key.json").write_text(json.dumps(seed_key))
        self.process = subprocess.Popen(
            [str(binary), str(objects), str(tmp / "seed-key.json"), str(tmp / "keys"), "--local"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: [lines.put(line) for line in self.process.stdout], daemon=True).start()
        self.ready = json.loads(lines.get(timeout=30))
        assert self.ready["ready"] is True and self.ready["direct_addrs"]

    def spec(self) -> dict:
        return {
            "endpoint_id": self.ready["endpoint_id"],
            "verification_key_digest": self.ready["verification_key_digest"],
            "direct_addrs": self.ready["direct_addrs"],
        }

    def stop(self) -> None:
        self.process.kill()
        self.process.wait(timeout=10)


def _database(seed: Seed) -> Path:
    return Path(seed.coordinator._state.database)  # noqa: SLF001 - the seed's own store


def _issue(seed: Seed, source: Source, node_id: str) -> dict:
    signer = seed.coordinator.signer
    return ra.issue_route_assignment(
        _database(seed),
        node_id=node_id,
        stage_pack=qm.sign_stage_manifest(V2["manifest"], signer),
        peer_endpoint_id=PEER,
        route_generation=7,
        seed_key_record=signer.public_key_record(),
        artifact_sources=[source.spec()],
    )


def test_the_node_acquires_verifies_and_applies_a_v2_stage(
    seed: Seed, node_cli: Path, source_bin: Path, tmp_path: Path  # noqa: F811
) -> None:
    source = Source(source_bin, tmp_path / "source", seed.coordinator.signer.public_key_record())
    node = Node(node_cli, tmp_path / "node", seed.pairing.channel_qr, COOL)
    try:
        node.wait(_held(1), 20, "join")
        record = _issue(seed, source, seed.member()["node_id"])
        applied = node.wait(lambda s: s["route_assignment"] == record["assignment_id"], 60, "the v2 stage to be applied")
        assert applied["stage_loaded"] == V2["manifest"]["pack_digest"]
        assert applied["artifact_state"] == "loaded"
        assert applied["route_ready"] is False
        objects = tmp_path / "node" / "artifacts" / "objects"
        assert {p.name for p in objects.iterdir()} == {t["sha256"][7:] for t in V2["manifest"]["tensors"]}
        assert not list((tmp_path / "node" / "artifacts" / "partial").iterdir())
        ra.withdraw_route_assignment(_database(seed), node_id=seed.member()["node_id"])
        cleared = node.wait(lambda s: s["route_assignment"] is None, 15, "the withdrawal")
        assert cleared["stage_loaded"] is None and cleared["artifact_state"] is None
    finally:
        node.stop()
        source.stop()


def test_a_hot_node_never_fetches_or_applies_the_v2_stage(
    seed: Seed, node_cli: Path, source_bin: Path, tmp_path: Path  # noqa: F811
) -> None:
    source = Source(source_bin, tmp_path / "source", seed.coordinator.signer.public_key_record())
    node = Node(node_cli, tmp_path / "node", seed.pairing.channel_qr, HOT)
    try:
        node.wait(_held(1), 20, "join")
        _issue(seed, source, seed.member()["node_id"])
        paused = node.wait(
            lambda s: (s.get("artifact_state") or "").startswith("paused:thermal_ceiling_reached"), 30, "the thermal pause"
        )
        assert paused["route_assignment"] is None and paused["stage_loaded"] is None
        assert paused["route_assignment_error"] == "route_assignment_stage_artifacts_pending"
        objects = tmp_path / "node" / "artifacts" / "objects"
        assert not list(objects.iterdir()), "nothing was fetched while hot"
    finally:
        node.stop()
        source.stop()
