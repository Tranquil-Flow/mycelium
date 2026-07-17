from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from layer_assignment import validate_assignment_identity
from mycelium_layer_planner.serialization import route_plan_from_dict
from route_contract import validate_manual_provisioning_route_v1
import scripts.contract_audit as contract_audit
import scripts.contract_io as contract_io
from scripts.contract_audit import audit
from scripts.contract_io import atomic_write_under_root, read_under_root
from scripts.generate_contract_fixtures import assignments_and_reports, manual_route as generated_manual_route
from scripts.generate_contract_manifest import encoded as encoded_manifest
from weight_provisioning import artifact_report_errors, provisioning_audit_errors


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "contracts" / "compatibility-fixtures"
EXPECTED_PROTOCOLS = {
    "route-plan-v2.json": "mycelium.route_plan.v2",
    "manual-provisioning-route-v1.json": "mycelium.manual_provisioning_route.v1",
    "layer-assignment-v2.json": "mycelium.layer_assignment.v2",
    "artifact-verification-report-v1.json": "mycelium.artifact_verification_report.v1",
    "provisioning-audit-v1.json": "mycelium.provisioning_audit.v1",
    "gossip-router-view-v1.json": "mycelium.gossip.router_view.v1",
    "gossip-allocator-view-v1.json": "mycelium.gossip.allocator_view.v1",
}


def isolated_contract_root(tmp_path: Path) -> tuple[Path, Path, dict]:
    """Copy the currently generated, valid contract tree without following test links."""
    manifest_bytes = encoded_manifest()
    manifest = json.loads(manifest_bytes)
    root = tmp_path.resolve() / "isolated-root"
    pins = []
    for contract in manifest["contracts"]:
        pins.append(contract["fixture"])
        pins.extend(contract["owner_sources"])
    for relative_path in {pin["path"] for pin in pins}:
        destination = root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative_path).read_bytes())
    manifest_path = root / "contracts" / "contract-manifest.v1.json"
    manifest_path.write_bytes(manifest_bytes)
    return root, manifest_path, manifest


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_generated_contract_fixtures_have_no_drift() -> None:
    for script in ("scripts/generate_contract_fixtures.py", "scripts/generate_contract_manifest.py"):
        completed = subprocess.run(
            [sys.executable, script, "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout


def test_contract_audit_checks_manifest_hashes_and_protocol_ownership() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/contract_audit.py", "--json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload == {"checked": 7, "drift": [], "ok": True}


def test_contract_audit_fails_closed_on_hash_namespace_and_path_drift(tmp_path: Path) -> None:
    canonical = json.loads((ROOT / "contracts" / "contract-manifest.v1.json").read_text(encoding="utf-8"))

    canonical["contracts"][0]["fixture"]["size_bytes"] += 1
    hash_manifest = tmp_path / "hash-drift.json"
    hash_manifest.write_text(json.dumps(canonical), encoding="utf-8")
    hash_result = audit(hash_manifest)
    assert hash_result["ok"] is False
    assert any("size drift" in error for error in hash_result["drift"])

    canonical = json.loads((ROOT / "contracts" / "contract-manifest.v1.json").read_text(encoding="utf-8"))
    canonical["contracts"][1]["protocol"] = canonical["contracts"][0]["protocol"]
    canonical["contracts"][1]["owner_sources"][0]["path"] = "../mycelium-mobile-lab/pyproject.toml"
    ownership_manifest = tmp_path / "ownership-drift.json"
    ownership_manifest.write_text(json.dumps(canonical), encoding="utf-8")
    ownership_result = audit(ownership_manifest)
    assert ownership_result["ok"] is False
    assert any("duplicate protocol owner" in error for error in ownership_result["drift"])
    assert any("escapes canonical root" in error for error in ownership_result["drift"])


def test_contract_audit_rejects_symlink_manifest(tmp_path: Path) -> None:
    alias = tmp_path / "manifest-alias.json"
    alias.symlink_to(ROOT / "contracts" / "contract-manifest.v1.json")

    result = audit(alias)

    assert result["ok"] is False
    assert any("symlink" in error for error in result["drift"])


def test_contract_audit_rejects_symlink_manifest_parent(tmp_path: Path) -> None:
    real_parent = tmp_path.resolve() / "real-parent"
    real_parent.mkdir()
    (real_parent / "manifest.json").write_bytes(encoded_manifest())
    alias_parent = tmp_path.resolve() / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    result = audit(alias_parent / "manifest.json")

    assert result["ok"] is False
    assert any("symlink" in error for error in result["drift"])


def test_contract_audit_rejects_manifest_listed_symlink_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, manifest_path, manifest = isolated_contract_root(tmp_path)
    relative_source = manifest["contracts"][0]["owner_sources"][0]["path"]
    source = root / relative_source
    backing = tmp_path.resolve() / "valid-source-backing"
    backing.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(backing)
    monkeypatch.setattr(contract_audit, "ROOT", root)

    result = audit(manifest_path)

    assert result["ok"] is False
    assert any("owner_sources" in error and "symlink" in error for error in result["drift"])


def test_contract_audit_rejects_manifest_listed_symlink_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, manifest_path, manifest = isolated_contract_root(tmp_path)
    relative_fixture = manifest["contracts"][0]["fixture"]["path"]
    fixture = root / relative_fixture
    backing = tmp_path.resolve() / "valid-fixture-backing.json"
    backing.write_bytes(fixture.read_bytes())
    fixture.unlink()
    fixture.symlink_to(backing)
    monkeypatch.setattr(contract_audit, "ROOT", root)

    result = audit(manifest_path)

    assert result["ok"] is False
    assert any("fixture" in error and "symlink" in error for error in result["drift"])


@pytest.mark.parametrize("pin_kind", ["owner_source", "fixture"])
def test_contract_audit_rejects_manifest_listed_symlink_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin_kind: str
) -> None:
    root, manifest_path, manifest = isolated_contract_root(tmp_path)
    if pin_kind == "owner_source":
        relative_path = next(
            source["path"]
            for contract in manifest["contracts"]
            for source in contract["owner_sources"]
            if len(Path(source["path"]).parts) > 1
        )
        expected_label = "owner_sources"
    else:
        relative_path = manifest["contracts"][0]["fixture"]["path"]
        expected_label = "fixture"
    pinned_path = root / relative_path
    real_parent = tmp_path.resolve() / f"real-{pin_kind}-parent"
    pinned_path.parent.rename(real_parent)
    pinned_path.parent.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.setattr(contract_audit, "ROOT", root)

    result = audit(manifest_path)

    assert result["ok"] is False
    assert any(expected_label in error and "symlink" in error for error in result["drift"])


def test_contract_audit_rejects_incomplete_alias_and_duplicate_key_manifests(tmp_path: Path) -> None:
    manifest_path = ROOT / "contracts" / "contract-manifest.v1.json"
    canonical_text = manifest_path.read_text(encoding="utf-8")
    canonical = json.loads(canonical_text)

    canonical["contracts"].pop()
    omitted_path = tmp_path / "omitted.json"
    omitted_path.write_text(json.dumps(canonical), encoding="utf-8")
    omitted = audit(omitted_path)
    assert omitted["ok"] is False
    assert any("contract registry mismatch" in error for error in omitted["drift"])

    canonical = json.loads(canonical_text)
    original = canonical["contracts"][0]["owner_sources"][0]["path"]
    canonical["contracts"][0]["owner_sources"][0]["path"] = f"./{original}"
    alias_path = tmp_path / "alias.json"
    alias_path.write_text(json.dumps(canonical), encoding="utf-8")
    alias = audit(alias_path)
    assert alias["ok"] is False
    assert any("canonical relative path" in error for error in alias["drift"])

    duplicate_path = tmp_path / "duplicate-key.json"
    duplicate_path.write_text(
        canonical_text.replace(
            "{\n",
            '{\n  "protocol": "wrong",\n',
            1,
        ),
        encoding="utf-8",
    )
    duplicate = audit(duplicate_path)
    assert duplicate["ok"] is False
    assert any("duplicate JSON key" in error for error in duplicate["drift"])


def test_contract_audit_reports_unhashable_manifest_fields_without_crashing(tmp_path: Path) -> None:
    canonical = json.loads((ROOT / "contracts" / "contract-manifest.v1.json").read_text(encoding="utf-8"))
    canonical["contracts"][0]["protocol"] = ["mycelium.invalid"]
    protocol_path = tmp_path / "array-protocol.json"
    protocol_path.write_text(json.dumps(canonical), encoding="utf-8")

    protocol_result = audit(protocol_path)

    assert protocol_result["ok"] is False
    assert any("contract registry mismatch" in error for error in protocol_result["drift"])
    assert any("invalid protocol" in error for error in protocol_result["drift"])

    canonical = json.loads((ROOT / "contracts" / "contract-manifest.v1.json").read_text(encoding="utf-8"))
    canonical["contracts"][0]["fixture"]["path"] = {"not": "a path"}
    fixture_path = tmp_path / "object-fixture-path.json"
    fixture_path.write_text(json.dumps(canonical), encoding="utf-8")

    fixture_result = audit(fixture_path)

    assert fixture_result["ok"] is False
    assert any("contract registry mismatch" in error for error in fixture_result["drift"])
    assert any("pinned path" in error for error in fixture_result["drift"])


def test_contract_io_rejects_symlink_reads_and_writes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    link = root / "contract.json"
    link.symlink_to(outside)

    try:
        atomic_write_under_root(root, link, b"replacement")
    except ValueError as error:
        assert "symlink" in str(error)
    else:
        raise AssertionError("symlink output must fail closed")

    try:
        read_under_root(root, link)
    except ValueError as error:
        assert "symlink" in str(error) or "escapes" in str(error)
    else:
        raise AssertionError("symlink input must fail closed")
    assert outside.read_bytes() == b"outside"


def test_contract_io_rejects_non_regular_inputs_and_outputs(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "root"
    root.mkdir()
    directory = root / "directory.json"
    directory.mkdir()
    fifo = root / "fifo.json"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="regular"):
        read_under_root(root, directory)
    with pytest.raises(ValueError, match="regular"):
        read_under_root(root, fifo)
    with pytest.raises(ValueError, match="regular"):
        atomic_write_under_root(root, directory, b"replacement")
    with pytest.raises(ValueError, match="regular"):
        atomic_write_under_root(root, fifo, b"replacement")

    device = Path("/dev/null")
    if device.exists() and stat.S_ISCHR(device.stat().st_mode):
        with pytest.raises(ValueError, match="regular"):
            read_under_root(device.parent, device)
        with pytest.raises(ValueError, match="regular"):
            atomic_write_under_root(device.parent, device, b"replacement")


def test_contract_io_always_attempts_parent_fd_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve() / "root"
    root.mkdir()
    target = root / "contract.json"
    target.write_bytes(b"content")
    real_close = os.close

    def exercise(operation) -> list[int]:
        parent_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        closed: list[int] = []

        def opened_parent(_root: Path, _target: Path) -> tuple[int, str]:
            return parent_fd, target.name

        def close_then_fail_once(descriptor: int) -> None:
            real_close(descriptor)
            closed.append(descriptor)
            if len(closed) == 1:
                raise OSError("simulated close failure")

        with monkeypatch.context() as context:
            context.setattr(contract_io, "_open_parent_under_root", opened_parent)
            context.setattr(contract_io.os, "close", close_then_fail_once)
            with pytest.raises(OSError, match="simulated close failure"):
                operation(root, target)
        return closed

    read_closed = exercise(lambda root, target: read_under_root(root, target))
    assert len(read_closed) == 2

    write_closed = exercise(
        lambda root, target: atomic_write_under_root(root, target, b"replacement")
    )
    assert len(write_closed) == 2


def test_atomic_write_keeps_replacement_inside_opened_parent_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve() / "root"
    parent = root / "contracts"
    parent.mkdir(parents=True)
    target = parent / "contract.json"
    target.write_bytes(b"original")
    displaced_parent = root / "displaced-contracts"
    outside_parent = tmp_path.resolve() / "outside"
    outside_parent.mkdir()
    outside_target = outside_parent / target.name
    outside_target.write_bytes(b"outside")
    real_open = os.open
    swapped = False

    def swap_parent_before_temporary_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and flags & os.O_WRONLY and flags & os.O_CREAT:
            parent.rename(displaced_parent)
            parent.symlink_to(outside_parent, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(contract_io.os, "open", swap_parent_before_temporary_open)

    atomic_write_under_root(root, target, b"replacement")

    assert swapped is True
    assert outside_target.read_bytes() == b"outside"
    assert (displaced_parent / target.name).read_bytes() == b"replacement"


def test_fixture_check_rejects_unexpected_files() -> None:
    unexpected = FIXTURES / "unexpected-contract.json"
    unexpected.write_text("{}\n", encoding="utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, "scripts/generate_contract_fixtures.py", "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode != 0
        assert "unexpected fixture" in (completed.stderr + completed.stdout)
    finally:
        unexpected.unlink(missing_ok=True)


def test_compatibility_fixtures_are_accepted_by_executable_consumers() -> None:
    observed = {name: load_fixture(name)["protocol"] for name in EXPECTED_PROTOCOLS}
    assert observed == EXPECTED_PROTOCOLS
    assert len(set(observed.values())) == len(observed)

    route_plan_from_dict(load_fixture("route-plan-v2.json"))
    manual_route = load_fixture("manual-provisioning-route-v1.json")
    validate_manual_provisioning_route_v1(manual_route)

    assignment = load_fixture("layer-assignment-v2.json")
    validate_assignment_identity(assignment)
    report = load_fixture("artifact-verification-report-v1.json")
    assert artifact_report_errors(assignment, report) == []

    assignments, reports = assignments_and_reports()
    audit_payload = load_fixture("provisioning-audit-v1.json")
    assert provisioning_audit_errors(generated_manual_route(), assignments, reports, audit_payload) == []
    tampered_audit = dict(audit_payload)
    tampered_audit["deployment_id"] = "87654321-4321-8765-4321-876543218765"
    assert any(
        "deployment_id" in error
        for error in provisioning_audit_errors(
            generated_manual_route(), assignments, reports, tampered_audit
        )
    )

    router_view = load_fixture("gossip-router-view-v1.json")
    allocator_view = load_fixture("gossip-allocator-view-v1.json")
    assert router_view["snapshot_generation"] == allocator_view["snapshot_generation"]
    assert router_view["nodes"]
    assert allocator_view["nodes"]

    nodes = {node["node_id"]: node for node in router_view["nodes"]}
    for edge in router_view["edges"]:
        assert edge["src_node_id"] in nodes
        assert edge["dst_node_id"] in nodes
        if edge["eligible"]:
            destination = nodes[edge["dst_node_id"]]
            assert destination["eligible"]
            assert edge["dst_endpoint_id"] in {
                endpoint["endpoint_id"] for endpoint in destination["endpoints"]
            }
