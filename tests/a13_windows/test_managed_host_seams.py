# SPDX-License-Identifier: AGPL-3.0-only
"""A13 managed-host helpers on the win32 backend (FakeWin32Api; no Windows host)."""

from __future__ import annotations

import pytest

import mycelium_host_platform
from mycelium_a13_macos import authority
from mycelium_a13_macos.authority import DemoAuthorityError
from mycelium_a13_macos.control_sequence import next_target_sequence

from tests.host_platform.fake_win32 import OTHER_USER_SID, owner_only, simulate_win32


def test_private_json_round_trip_and_refusals_on_win32(monkeypatch, tmp_path):
    fake = simulate_win32(monkeypatch)
    root = authority._private_directory(tmp_path / "target", create=True)
    path = root / "state.json"
    assert authority._load_private_json(path) is None
    authority._atomic_private_json(path, {"b": 1, "a": [1, 2]})
    assert authority._load_private_json(path) == {"a": [1, 2], "b": 1}
    fake.set_security(path, owner_only(OTHER_USER_SID))
    with pytest.raises(DemoAuthorityError):
        authority._load_private_json(path)
    with pytest.raises(DemoAuthorityError):
        authority._private_directory(tmp_path / "absent", create=False)


def test_control_sequence_is_durable_and_monotonic_on_win32(monkeypatch, tmp_path):
    simulate_win32(monkeypatch)
    root = authority._private_directory(tmp_path / "target", create=True)
    digest = "sha256:" + "a" * 64
    first = next_target_sequence(root, signer_key_digest=digest, swarm_id="s", at_least=0)
    second = next_target_sequence(root, signer_key_digest=digest, swarm_id="s", at_least=0)
    jumped = next_target_sequence(root, signer_key_digest=digest, swarm_id="s", at_least=40)
    assert (first, second, jumped) == (1, 2, 41)
    with pytest.raises(ValueError, match="control_sequence_scope_invalid"):
        next_target_sequence(root, signer_key_digest="sha256:" + "b" * 64, swarm_id="s", at_least=0)


def test_control_sequence_lock_is_exclusive_on_win32(monkeypatch, tmp_path):
    simulate_win32(monkeypatch)
    root = authority._private_directory(tmp_path / "target", create=True)
    held = mycelium_host_platform.hold_exclusive_lock(root, "control-sequence.lock")
    try:
        # The fake cannot block, so a contended lock surfaces as a refusal.
        with pytest.raises(ValueError, match="control_sequence_invalid"):
            next_target_sequence(root, signer_key_digest="sha256:" + "a" * 64, swarm_id="s", at_least=0)
    finally:
        held.release()
    assert next_target_sequence(root, signer_key_digest="sha256:" + "a" * 64, swarm_id="s", at_least=0) == 1
