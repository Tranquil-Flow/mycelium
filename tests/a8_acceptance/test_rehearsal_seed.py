# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rehearsal-seed launcher tests: loopback-only bind, public-origin pinning,
durable seed identity across processes, owner-private state, and an
explicit rehearsal label that never claims qualification."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.a8_run_rehearsal_seed import (
    DEFAULT_LOOPBACK_PORT,
    REHEARSAL_LABEL,
    SEED_NODE_ID,
    _durable_seed_id_source,
    build_rehearsal_seed,
    load_rehearsal_signer,
    mint_rehearsal_invite,
)

ORIGIN = "https://seed.example.test"


def test_seed_id_source_continues_across_restarts(tmp_path: Path) -> None:
    """A restart must never reuse a persisted message id.

    ``seed_emitted_messages`` survives restarts while an in-process counter
    does not; a naive ``count(1)`` id source fails the next join with
    ``seed_message_id_reused`` (observed live against the public origin).
    """

    import sqlite3

    state_root = tmp_path / "rehearsal-seed"
    state_root.mkdir(mode=0o700)
    connection = sqlite3.connect(state_root / "state.sqlite3")
    connection.execute(
        "CREATE TABLE seed_emitted_messages (message_id TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO seed_emitted_messages (message_id) VALUES (?)",
        (f"{SEED_NODE_ID}-41",),
    )
    connection.commit()
    connection.close()

    id_source = _durable_seed_id_source(state_root)
    assert id_source() == f"{SEED_NODE_ID}-42"
    assert id_source() == f"{SEED_NODE_ID}-43"


def test_rehearsal_label_never_claims_qualification() -> None:
    assert "rehearsal" in REHEARSAL_LABEL
    assert "qualification" not in REHEARSAL_LABEL


def test_default_port_never_collides_with_the_m14_seed() -> None:
    assert DEFAULT_LOOPBACK_PORT != 8876
    assert 1024 < DEFAULT_LOOPBACK_PORT < 65536


def test_server_binds_loopback_only_with_pinned_origin(tmp_path: Path) -> None:
    state_root = tmp_path / "rehearsal-seed"
    server, coordinator, policy = build_rehearsal_seed(
        origin=ORIGIN,
        state_root=state_root,
        port=0,
    )
    try:
        assert server.base_url.startswith("http://127.0.0.1:")
        assert coordinator.seed_url == ORIGIN
        assert policy.canonical_origin == ORIGIN
        assert state_root.exists()
        assert state_root.stat().st_mode & 0o022 == 0
        assert (state_root / "state.sqlite3").exists()
        assert (state_root / "identity" / "seed.key").exists()
        assert (state_root / "identity").stat().st_mode & 0o022 == 0
    finally:
        server.close()


def test_seed_identity_is_durable_across_loads(tmp_path: Path) -> None:
    state_root = tmp_path / "rehearsal-seed"
    server, _, _ = build_rehearsal_seed(
        origin=ORIGIN,
        state_root=state_root,
        port=0,
    )
    server.close()
    first = load_rehearsal_signer(state_root)
    second = load_rehearsal_signer(state_root)
    assert first.verification_key_digest == second.verification_key_digest


def test_mint_binds_invite_to_the_public_origin(tmp_path: Path) -> None:
    import time

    from mycelium_invite import verify_invite_bundle

    state_root = tmp_path / "rehearsal-seed"
    server, _, _ = build_rehearsal_seed(
        origin=ORIGIN,
        state_root=state_root,
        port=0,
    )
    server.close()
    bundle, bundle_path = mint_rehearsal_invite(
        origin=ORIGIN,
        state_root=state_root,
        nonce="test-invite",
    )
    verified = verify_invite_bundle(bundle, now=time.time())
    assert verified["payload"]["seed_url"] == ORIGIN
    assert bundle_path.exists()
    assert bundle_path.stat().st_mode & 0o777 == 0o600


def test_origin_must_be_canonical_https(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_rehearsal_seed(
            origin="http://seed.example.test",
            state_root=tmp_path / "bad",
            port=0,
        )
