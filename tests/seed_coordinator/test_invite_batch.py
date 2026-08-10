from __future__ import annotations

from itertools import count
import hashlib
import json
import math
from pathlib import Path
import stat

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_seed import (
    InviteBatchError,
    SeedCoordinator,
    mint_invite_batch,
)
from mycelium_seed.http import SeedHTTPClient, SeedHTTPServer


NOW = 4_000.0


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def _seed(tmp_path: Path) -> tuple[Path, SeedCoordinator]:
    seed_root = tmp_path / "seed"
    seed_root.mkdir(mode=0o700)
    signer = load_or_create_node_signer(seed_root / "identity" / "seed.key")
    coordinator = SeedCoordinator(
        swarm_id="swarm-batch",
        seed_node_id="seed-node",
        seed_url=None,
        signer=signer,
        invite_registry=SqliteInviteRegistry(seed_root / "state.sqlite3"),
        incarnation="seed-batch",
        clock=lambda: NOW,
        id_source=_ids("seed-message"),
    )
    return seed_root, coordinator


def _node(tmp_path: Path, index: int) -> NodeMembershipSession:
    return NodeMembershipSession(
        node_id=f"device-{index:03d}",
        swarm_id="swarm-batch",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(
            tmp_path / "nodes" / f"device-{index:03d}.key"
        ),
        incarnation=f"device-{index:03d}-first",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=lambda: NOW,
        id_source=_ids(f"device-{index:03d}-message"),
    )


def test_batch_mints_private_unique_invites_and_eight_devices_join(
    tmp_path: Path,
) -> None:
    seed_root, coordinator = _seed(tmp_path)
    with SeedHTTPServer(coordinator, host="127.0.0.1", port=0) as server:
        status = mint_invite_batch(
            seed_data_dir=seed_root,
            seed_url=server.base_url,
            swarm_id="swarm-batch",
            output_root=tmp_path / "outbox",
            count=8,
            ttl_seconds=120,
            now=lambda: NOW,
            batch_id_source=lambda: "batch-one",
        )

        assert status["invite_count"] == 8
        assert status["route_ready"] is False
        batch = Path(status["output_directory"])
        assert stat.S_IMODE(batch.stat().st_mode) == 0o700
        manifest_path = batch / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        assert status["manifest_digest"] == (
            "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        )
        manifest = json.loads(manifest_bytes)
        assert manifest["activation_eligible_after_join"] is False
        assert manifest["invite_count"] == 8
        assert len(manifest["files"]) == 8

        seen_nonces: set[str] = set()
        seen_keys: set[str] = set()
        raw_tokens: list[str] = []
        for index, entry in enumerate(manifest["files"], start=1):
            invite_path = batch / entry["file"]
            body = invite_path.read_bytes()
            assert stat.S_IMODE(invite_path.stat().st_mode) == 0o600
            assert entry["digest"] == "sha256:" + hashlib.sha256(body).hexdigest()
            bundle = json.loads(body)
            raw_tokens.append(bundle["token"])
            verified = verify_invite_bundle(bundle, now=NOW)
            nonce = verified["payload"]["nonce"]
            assert nonce not in seen_nonces
            seen_nonces.add(nonce)

            node = _node(tmp_path, index)
            request = node.join_request(
                invite_nonce=nonce,
                endpoint_addrs=[f"https://device-{index:03d}.example/control"],
            )
            client = SeedHTTPClient.from_invite_bundle(bundle, now=NOW, timeout=2)
            acceptance = client.join(
                invite_token=bundle["token"],
                join_envelope=request,
            )
            node.accept_join(
                acceptance,
                seed_key_digest=verified["seed_key_digest"],
            )
            seen_keys.add(node.signer.verification_key_digest)

        manifest_text = manifest_path.read_text(encoding="utf-8")
        assert all(token not in manifest_text for token in raw_tokens)
        assert len(seen_nonces) == 8
        assert len(seen_keys) == 8
        assert len(coordinator.members()) == 8
        assert all(member["generation"] == 1 for member in coordinator.members())


@pytest.mark.parametrize("count", [0, 65, True])
def test_batch_rejects_unbounded_counts(tmp_path: Path, count: int) -> None:
    with pytest.raises(InviteBatchError, match="invite_batch_count_invalid"):
        mint_invite_batch(
            seed_data_dir=tmp_path / "missing",
            seed_url="http://127.0.0.1:8765",
            swarm_id="swarm-batch",
            output_root=tmp_path / "outbox",
            count=count,
            ttl_seconds=120,
        )


@pytest.mark.parametrize("clock", [lambda: math.nan, lambda: math.inf])
def test_batch_rejects_nonfinite_clock(tmp_path: Path, clock) -> None:
    with pytest.raises(InviteBatchError, match="invite_batch_clock_invalid"):
        mint_invite_batch(
            seed_data_dir=tmp_path / "missing",
            seed_url="http://127.0.0.1:8765",
            swarm_id="swarm-batch",
            output_root=tmp_path / "outbox",
            count=1,
            ttl_seconds=120,
            now=clock,
        )


def test_unverified_seed_creates_no_batch_directory(tmp_path: Path) -> None:
    seed_root, coordinator = _seed(tmp_path)
    output_root = tmp_path / "outbox"
    with SeedHTTPServer(coordinator, host="127.0.0.1", port=0) as server:
        with pytest.raises(InviteBatchError, match="invite_batch_seed_unverified"):
            mint_invite_batch(
                seed_data_dir=seed_root,
                seed_url=server.base_url,
                swarm_id="different-swarm",
                output_root=output_root,
                count=2,
                ttl_seconds=120,
                now=lambda: NOW,
                batch_id_source=lambda: "must-not-exist",
            )
    assert not output_root.exists()


def test_batch_files_are_not_overwritten(tmp_path: Path) -> None:
    seed_root, coordinator = _seed(tmp_path)
    with SeedHTTPServer(coordinator, host="127.0.0.1", port=0) as server:
        arguments = {
            "seed_data_dir": seed_root,
            "seed_url": server.base_url,
            "swarm_id": "swarm-batch",
            "output_root": tmp_path / "outbox",
            "count": 1,
            "ttl_seconds": 120,
            "now": lambda: NOW,
            "batch_id_source": lambda: "same-batch",
        }
        first = mint_invite_batch(**arguments)
        first_bytes = (Path(first["output_directory"]) / "manifest.json").read_bytes()
        with pytest.raises(InviteBatchError, match="invite_batch_output_invalid"):
            mint_invite_batch(**arguments)
        assert (Path(first["output_directory"]) / "manifest.json").read_bytes() == first_bytes
