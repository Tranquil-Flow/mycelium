# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the fresh operator-plan template compiler input factory.

Scope guard: every test here is a metadata/shape unit test against
``mycelium_a13_macos.compiler_inputs`` and the canonical strict parser
``mycelium_physical_runner.config.parse_operator_plan``.  Nothing in this
module loads a model, opens a network socket, creates signer/seed authority,
or performs physical execution.  A canonical-parse success in the
``final_shape`` test proves document SHAPE only; it is never physical
execution evidence and must never be cited as qualification.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import os
from pathlib import Path
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mycelium_a13_macos.compiler_inputs import (
    CompilerInputsError,
    OPERATOR_PLAN_PROTOCOL,
    fresh_operator_template,
)
from mycelium_physical_runner.config import (
    OPERATOR_PLAN_PROTOCOL as CANONICAL_PROTOCOL,
)
from mycelium_physical_runner.errors import RunnerError
from mycelium_physical_runner.config import parse_operator_plan


# ---------------------------------------------------------------------------
# Deterministic unit-fixture public keys (no signer is ever created; these
# are fixed 32-byte values with digests computed inline).
# ---------------------------------------------------------------------------

def _record(raw: bytes, endpoint_id: str | None = None) -> dict:
    record = {
        "algorithm": "ed25519",
        "encoding": "base64",
        "verification_key": base64.b64encode(raw).decode("ascii"),
        "verification_key_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }
    if endpoint_id is not None:
        record["endpoint_id"] = endpoint_id
    return record


_KEY_A = bytes(range(32))
_KEY_B = bytes(range(32, 64))
_KEY_C = bytes(range(64, 96))
PUBLIC_KEYS = {
    "gossip": [_record(_KEY_A, endpoint_id="unit-fixture-ep-a")],
    "load_proof": [_record(_KEY_B), _record(_KEY_C)],
}


def _public_keys() -> dict:
    return copy.deepcopy(PUBLIC_KEYS)


# ---------------------------------------------------------------------------
# Import resolution: the module under test must come from this snapshot.
# ---------------------------------------------------------------------------


def test_module_resolves_to_this_snapshot() -> None:
    import mycelium_a13_macos.compiler_inputs as module

    module_root = Path(module.__file__).resolve().parents[1]
    assert module_root == _REPO_ROOT
    assert _REPO_ROOT == Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Positive shape: uncompiled intent, not a runnable plan.
# ---------------------------------------------------------------------------


def test_template_shape_and_bound_paths(tmp_path: Path) -> None:
    root = tmp_path / "private-run-root"
    template = fresh_operator_template(root, _public_keys())

    assert template["protocol"] == CANONICAL_PROTOCOL
    assert template["plan_id"] == "pending-plan"
    assert template["run_id"] == "pending-run"
    assert template["now_unix_ms"] == 0

    paths = template["paths"]
    assert set(paths) == {
        "evidence_output_dir",
        "lock_path",
        "state_path",
        "log_path",
    }
    values = [paths[name] for name in sorted(paths)]
    assert len(set(values)) == 4
    for value in values:
        assert Path(value).is_absolute()
        assert value.startswith(str(root) + "/")

    controller = template["controller"]
    assert controller["mode"] == "physical"
    assert controller["source_root"].startswith(str(root) + "/")
    assert controller["transfer_manifest"] == {
        "protocol": "mycelium.controller_transfer_manifest.v1",
        "files": [],
    }
    assert controller["membership_snapshot"]["protocol"].endswith(".pending.v1")
    assert controller["run_plan"]["protocol"].endswith(".pending.v1")

    peers = controller["peers"]
    assert len(peers) == 2
    node_ids = [peer["node_id"] for peer in peers]
    assert len(set(node_ids)) == 2
    for peer in peers:
        assert set(peer) == {
            "node_id",
            "ssh_target",
            "ssh_identity_file",
            "host_id",
            "boot_id",
            "staging_root",
            "process_transport",
        }
        assert peer["process_transport"] == "managed_https"
        assert peer["ssh_target"] is None
        assert peer["ssh_identity_file"] is None
        # Placeholders must never masquerade as measured host identities.
        assert "pending" in peer["host_id"]
        assert "pending" in peer["boot_id"]

    assert template["verification_keys"] == {
        "gossip": [dict(PUBLIC_KEYS["gossip"][0])],
        "load_proof": [dict(PUBLIC_KEYS["load_proof"][0]), dict(PUBLIC_KEYS["load_proof"][1])],
    }


def test_template_is_not_a_canonical_runnable_plan(tmp_path: Path) -> None:
    template = fresh_operator_template(
        tmp_path / "private-run-root", _public_keys()
    )
    with pytest.raises(RunnerError):
        parse_operator_plan(copy.deepcopy(template))


def test_template_construction_has_no_io_side_effects(tmp_path: Path) -> None:
    root = tmp_path / "private-run-root"
    root.mkdir()
    before = sorted(p.name for p in root.iterdir())
    template = fresh_operator_template(root, _public_keys())
    after = sorted(p.name for p in root.iterdir())
    assert before == after == []
    for value in template["paths"].values():
        assert not Path(value).exists()
    assert not Path(template["controller"]["source_root"]).exists()


def test_result_is_freshly_owned(tmp_path: Path) -> None:
    root = tmp_path / "private-run-root"
    first = fresh_operator_template(root, _public_keys())
    first["paths"]["lock_path"] = "/etc/tampered"
    first["verification_keys"]["gossip"][0]["algorithm"] = "rsa"
    second = fresh_operator_template(root, _public_keys())
    assert second["paths"]["lock_path"].startswith(str(root) + "/")
    assert second["verification_keys"]["gossip"][0]["algorithm"] == "ed25519"


# ---------------------------------------------------------------------------
# Final shape: fill builder/compiler-owned fields from declared unit-fixture
# metadata, then validate with the canonical parser.  Shape proof only.
# ---------------------------------------------------------------------------


def test_final_shape_passes_canonical_parser(tmp_path: Path) -> None:
    template = fresh_operator_template(
        tmp_path / "private-run-root", _public_keys()
    )
    source_root = tmp_path / "filled-source"
    source_root.mkdir()
    template["plan_id"] = "unit-fixture-plan"
    template["run_id"] = "unit-fixture-run"
    template["now_unix_ms"] = 1_800_000_000_000
    peers = template["controller"]["peers"]
    for peer, suffix in zip(peers, ("a", "b"), strict=True):
        peer["node_id"] = f"unit-fixture-node-{suffix}"
        peer["host_id"] = f"unit-fixture-host-{suffix}"
        peer["boot_id"] = f"unit-fixture-boot-{suffix}"
        peer["staging_root"] = f"/unit-fixture/staging/{suffix}"
    template["controller"]["source_root"] = str(source_root)
    template["controller"]["now"] = 1_800_000_000.0
    template["controller"]["membership_snapshot"] = {
        "protocol": "mycelium.controller_membership_snapshot.v1",
        "deployment_id": "unit-fixture-deployment",
    }
    template["controller"]["run_plan"] = {
        "protocol": "mycelium.controller_run_plan.v1",
        "run_id": "unit-fixture-run",
        "deployment_id": "unit-fixture-deployment",
        "entry_node_id": "unit-fixture-node-a",
        "nodes": [
            {"node_id": "unit-fixture-node-a"},
            {"node_id": "unit-fixture-node-b"},
        ],
    }
    config = parse_operator_plan(copy.deepcopy(template))
    assert config.protocol == CANONICAL_PROTOCOL
    assert config.plan_id == "unit-fixture-plan"
    assert config.run_id == "unit-fixture-run"
    assert config.evidence_output_dir.endswith("/evidence")
    assert config.lock_path.endswith("/run/runner.lock")
    assert config.state_path.endswith("/run/runner-state.json")
    assert config.log_path.endswith("/run/runner.log")
    assert config.controller["mode"] == "physical"
    assert config.gossip_verification_keys[0]["endpoint_id"] == "unit-fixture-ep-a"
    assert len(config.load_proof_verification_keys) == 2


# ---------------------------------------------------------------------------
# Rejection: unsafe private run roots.
# ---------------------------------------------------------------------------


def test_rejects_relative_root(tmp_path: Path) -> None:
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(Path("relative/root"), _public_keys())


def test_rejects_parent_traversal_root() -> None:
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(Path("/safe/root/../etc"), _public_keys())


def test_rejects_forbidden_root_prefix() -> None:
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(Path("/etc/mycelium"), _public_keys())


def test_rejects_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    os.symlink(real, link)
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(link, _public_keys())


def test_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    link_parent = tmp_path / "link-parent"
    os.symlink(real_parent, link_parent)
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(link_parent / "run-root", _public_keys())


def test_rejects_root_with_control_characters(tmp_path: Path) -> None:
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(str(tmp_path / "bad\nroot"), _public_keys())


# ---------------------------------------------------------------------------
# Rejection: invalid public-key material and secret leakage.
# ---------------------------------------------------------------------------


def test_rejects_missing_purpose() -> None:
    keys = _public_keys()
    del keys["load_proof"]
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(Path("/safe/root"), keys)


def test_rejects_unknown_purpose(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["extra"] = []
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_empty_purpose(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["gossip"] = []
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_unknown_record_field(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["gossip"][0]["note"] = "extra"
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_wrong_algorithm(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["gossip"][0]["algorithm"] = "rsa"
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_missing_encoding(tmp_path: Path) -> None:
    keys = _public_keys()
    del keys["gossip"][0]["encoding"]
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_non_base64_key(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["gossip"][0]["verification_key"] = "!!not-base64!!"
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_wrong_key_length(tmp_path: Path) -> None:
    keys = _public_keys()
    raw = bytes(31)
    keys["gossip"][0]["verification_key"] = base64.b64encode(raw).decode("ascii")
    keys["gossip"][0]["verification_key_digest"] = (
        "sha256:" + hashlib.sha256(raw).hexdigest()
    )
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_digest_mismatch(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["gossip"][0]["verification_key_digest"] = (
        "sha256:" + hashlib.sha256(_KEY_C).hexdigest()
    )
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_malformed_digest(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["gossip"][0]["verification_key_digest"] = "sha256:zz" * 16
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_credential_material_in_record(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["gossip"][0]["endpoint_id"] = "-----BEGIN PRIVATE KEY-----"
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_token_material_in_record(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["load_proof"][0]["endpoint_id"] = "hf-abcdefghijklmnopqrstuv"
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_duplicate_digests_within_purpose(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["load_proof"][1] = dict(keys["load_proof"][0])
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)


def test_rejects_secret_key_field_in_record(tmp_path: Path) -> None:
    keys = _public_keys()
    keys["gossip"][0]["private_key"] = "material"
    with pytest.raises(CompilerInputsError):
        fresh_operator_template(tmp_path, keys)
