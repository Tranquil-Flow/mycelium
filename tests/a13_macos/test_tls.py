# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pathlib import Path
import ssl

import pytest

from mycelium_a13_macos.tls import (
    LocalTLSSeedServer,
    ensure_local_tls_material,
    pinned_seed_client_from_invite,
)
from mycelium_invite import SqliteInviteRegistry
from mycelium_node import load_or_create_node_signer
from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError
from mycelium_seed.state import SqliteSeedState


def _coordinator(tmp_path: Path) -> SeedCoordinator:
    root = tmp_path / "seed"
    root.mkdir(mode=0o700)
    signer = load_or_create_node_signer(root / "identity" / "seed.key")
    database = root / "state.sqlite3"
    state = SqliteSeedState(database)
    return SeedCoordinator(
        swarm_id="local-tls-swarm",
        seed_node_id="seed-node",
        seed_url=None,
        signer=signer,
        invite_registry=SqliteInviteRegistry(database),
        incarnation="seed-incarnation",
        state=state,
    )


def test_actual_tls_socket_and_pinned_canonical_seed_client(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    coordinator = _coordinator(tmp_path)
    material = ensure_local_tls_material(tmp_path / "tls")
    with LocalTLSSeedServer(coordinator, material=material) as server:
        assert server.base_url.startswith("https://127.0.0.1:")
        assert isinstance(server.canonical_server._server.socket, ssl.SSLSocket)
        bundle = coordinator.mint_invite(nonce="tls-invite", ttl_seconds=120)
        client = pinned_seed_client_from_invite(
            bundle,
            now=coordinator._clock(),
            ca_certificate=material.ca_certificate,
            timeout=2,
        )
        assert type(client) is SeedHTTPClient
        identity = client.identity(now=coordinator._clock() + 1.0)
        assert identity["seed_url"] == server.base_url
        assert identity["swarm_id"] == "local-tls-swarm"

        unpinned = SeedHTTPClient.from_invite_bundle(
            bundle,
            now=coordinator._clock(),
            timeout=2,
        )
        with pytest.raises(SeedHTTPError, match="seed_http_unreachable"):
            unpinned.identity(now=coordinator._clock())


def test_tls_identity_is_durable_private_and_not_regenerated(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    first = ensure_local_tls_material(tmp_path / "tls")
    before = {
        path.name: path.read_bytes()
        for path in (
            first.ca_certificate,
            first.server_certificate,
            first.server_private_key,
        )
    }
    second = ensure_local_tls_material(tmp_path / "tls")
    assert second == first
    assert before == {
        path.name: path.read_bytes()
        for path in (
            second.ca_certificate,
            second.server_certificate,
            second.server_private_key,
        )
    }
    for path in (
        second.ca_certificate,
        second.server_certificate,
        second.server_private_key,
    ):
        assert path.stat().st_mode & 0o777 == 0o600


def test_missing_member_of_existing_tls_identity_fails_without_regeneration(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    material = ensure_local_tls_material(tmp_path / "tls")
    material.server_private_key.unlink()
    with pytest.raises(Exception, match="local_tls_material_incomplete"):
        ensure_local_tls_material(tmp_path / "tls")
    assert not material.server_private_key.exists()
