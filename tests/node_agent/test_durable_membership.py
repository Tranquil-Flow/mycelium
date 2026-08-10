from __future__ import annotations

from pathlib import Path
import os

import pytest

from mycelium_node.durable_membership import (
    DurableMembershipError,
    PROTOCOL,
    load_membership_state,
    next_incarnation,
    save_membership_state,
)
from mycelium_qualification.signing import generate_ed25519_signer


def _state() -> dict:
    signer = generate_ed25519_signer(endpoint_id="node-endpoint")
    return {
        "protocol": PROTOCOL,
        "node_id": "node-a",
        "swarm_id": "swarm-a",
        "seed_node_id": "seed-node",
        "seed_url": "http://127.0.0.1:8788",
        "seed_key_digest": signer.verification_key_digest,
        "seed_key_records": [signer.public_key_record()],
        "endpoint_id": "node-endpoint",
        "incarnation": "node-incarnation",
        "membership_generation": 3,
        "restart_count": 2,
    }


def test_membership_state_roundtrip_is_owner_only_and_atomic(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    expected = _state()
    assert load_membership_state(tmp_path) is None
    assert save_membership_state(tmp_path, expected) == expected
    assert load_membership_state(tmp_path) == expected
    assert (tmp_path / "membership.state.json").stat().st_mode & 0o777 == 0o600


def test_membership_state_rejects_unsafe_or_noncanonical_file(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "membership.state.json"
    path.write_text("{}\n")
    path.chmod(0o644)
    with pytest.raises(DurableMembershipError, match="node_membership_state_invalid"):
        load_membership_state(tmp_path)

    path.unlink()
    target = tmp_path / "target"
    target.write_text("{}")
    os.symlink(target, path)
    with pytest.raises(DurableMembershipError, match="node_membership_state_invalid"):
        load_membership_state(tmp_path)


def test_next_incarnation_is_stable_and_bounded() -> None:
    assert next_incarnation("node-incarnation", 4) == "node-incarnation.r4"
    assert len(next_incarnation("a" * 128, 5)) <= 128
