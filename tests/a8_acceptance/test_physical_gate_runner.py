# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic gates for the A8 physical-gate runner and qualification
sealer. The runner is inert and fail-closed without live infrastructure and
never writes evidence unless a case genuinely executes."""

from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from mycelium_internet.physical import (
    A8_PHYSICAL_CASES,
    PEER_REQUIRED_CASES,
    PeerRequired,
    PhysicalGateError,
    execute_case,
    preflight_document,
    seal_qualification,
)
from mycelium_internet.contracts import (
    INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
    validate_internet_native_qualification,
)

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "a8_run_physical_gate.py"
INVENTORY = ROOT / "tests" / "a8_acceptance" / "inventory.v1.json"
PLACEHOLDER = "sha256:" + "a" * 64
SOURCE_DIGEST = json.loads(INVENTORY.read_text("utf-8"))["physical_execution"][
    "source_digest"
]


def _inventory_cases() -> set[str]:
    inventory = json.loads(INVENTORY.read_text("utf-8"))
    return {
        case["case_id"]
        for section in ("physical_positive_cases", "physical_negative_cases")
        for case in inventory[section]
    }


def test_case_registry_matches_the_acceptance_inventory() -> None:
    assert set(A8_PHYSICAL_CASES) == _inventory_cases()


def test_peer_required_cases_are_exactly_the_external_peer_gates() -> None:
    expected = {
        "unrelated_https_invite_without_tailscale",
        "direct_path_qualified_browser_inference",
        "forced_relay_privacy_reduced_browser_inference",
        "observed_path_transition_and_reconnect",
        "revoked_active_member",
        "endpoint_identity_mismatch",
        "unqualified_external_member",
        "tailscale_unavailable",
        "ssh_unavailable",
    }
    assert set(PEER_REQUIRED_CASES) == expected


def test_browser_transport_cli_collects_repeatable_signed_reports(tmp_path: Path) -> None:
    from scripts.a8_run_physical_gate import _parser

    args = _parser().parse_args(
        [
            "run",
            "observed_path_transition_and_reconnect",
            "--origin",
            "https://seed.example.invalid",
            "--spec-digest",
            PLACEHOLDER,
            "--source-digest",
            PLACEHOLDER,
            "--browser-report-file",
            str(tmp_path / "browser.json"),
            "--browser-authority-file",
            str(tmp_path / "browser-authority.json"),
            "--transport-report-file",
            str(tmp_path / "direct.json"),
            "--transport-report-file",
            str(tmp_path / "relay.json"),
            "--relay-projection-key-file",
            str(tmp_path / "projection.key"),
        ]
    )
    assert args.browser_report_file == tmp_path / "browser.json"
    assert args.browser_authority_file == tmp_path / "browser-authority.json"
    assert args.transport_report_files == [
        tmp_path / "direct.json",
        tmp_path / "relay.json",
    ]
    assert args.relay_projection_key_file == tmp_path / "projection.key"


def test_endpoint_mismatch_cli_collects_exact_executor_inputs(tmp_path: Path) -> None:
    from scripts.a8_run_physical_gate import _parser

    args = _parser().parse_args(
        [
            "run",
            "endpoint_identity_mismatch",
            "--origin",
            "https://seed.example.invalid",
            "--spec-digest",
            PLACEHOLDER,
            "--source-digest",
            SOURCE_DIGEST,
            "--sidecar-binary",
            str(tmp_path / "sidecar"),
            "--receiver-endpoint-secret-file",
            str(tmp_path / "receiver.key"),
        ]
    )
    assert args.sidecar_binary == tmp_path / "sidecar"
    assert args.receiver_endpoint_secret_file == tmp_path / "receiver.key"


def test_endpoint_mismatch_runner_rejects_arbitrary_probe_program(
    tmp_path: Path,
) -> None:
    from scripts.a8_run_physical_gate import _endpoint_mismatch_probe_program

    assert _endpoint_mismatch_probe_program(None) == (
        ROOT / "scripts" / "a8_endpoint_mismatch_probe.py"
    ).resolve()
    arbitrary = tmp_path / "arbitrary-probe"
    arbitrary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    arbitrary.chmod(0o700)
    with pytest.raises(PhysicalGateError) as exc_info:
        _endpoint_mismatch_probe_program(arbitrary)
    assert exc_info.value.code == "physical_infrastructure_unavailable"


def test_endpoint_probe_runtime_files_bind_binary_and_private_receiver_key(
    tmp_path: Path,
) -> None:
    from scripts.a8_run_physical_gate import _endpoint_probe_runtime_files

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    binary = private / "sidecar"
    binary.write_bytes(b"release-sidecar")
    binary.chmod(0o700)
    receiver_key = private / "receiver.key"
    receiver_key.write_bytes(b"r" * 32)
    receiver_key.chmod(0o600)

    bound_binary, bound_key, digest = _endpoint_probe_runtime_files(
        sidecar_binary=binary,
        receiver_endpoint_secret_file=receiver_key,
    )
    assert bound_binary == binary.resolve()
    assert bound_key == receiver_key.resolve()
    assert digest == "sha256:" + hashlib.sha256(b"release-sidecar").hexdigest()

    receiver_key.chmod(0o644)
    with pytest.raises(PhysicalGateError) as exc_info:
        _endpoint_probe_runtime_files(
            sidecar_binary=binary,
            receiver_endpoint_secret_file=receiver_key,
        )
    assert exc_info.value.code == "physical_infrastructure_unavailable"


@pytest.mark.parametrize("missing", ["binary", "receiver"])
def test_endpoint_probe_runtime_files_reject_missing_inputs(
    tmp_path: Path,
    missing: str,
) -> None:
    from scripts.a8_run_physical_gate import _endpoint_probe_runtime_files

    binary = None if missing == "binary" else tmp_path / "sidecar"
    receiver = None if missing == "receiver" else tmp_path / "receiver.key"
    with pytest.raises(PhysicalGateError) as exc_info:
        _endpoint_probe_runtime_files(
            sidecar_binary=binary,
            receiver_endpoint_secret_file=receiver,
        )
    assert exc_info.value.code == "physical_infrastructure_unavailable"


def test_endpoint_only_runtime_options_are_rejected_for_other_cases(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.a8_run_physical_gate import main

    result = main(
        [
            "run",
            "cleartext_or_redirect_bootstrap",
            "--origin",
            "https://seed.example.invalid",
            "--spec-digest",
            PLACEHOLDER,
            "--source-digest",
            SOURCE_DIGEST,
            "--sidecar-binary",
            str(tmp_path / "sidecar"),
        ]
    )
    assert result == 2
    assert "gate rejected: case_unknown" in capsys.readouterr().err


def test_run_cli_requires_non_placeholder_source_and_spec_digests() -> None:
    from scripts.a8_run_physical_gate import _parser

    base = [
        "run",
        "cleartext_or_redirect_bootstrap",
        "--origin",
        "https://seed.example.invalid",
    ]
    with pytest.raises(SystemExit):
        _parser().parse_args(base)
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                *base,
                "--spec-digest",
                "sha256:" + "0" * 64,
                "--source-digest",
                PLACEHOLDER,
            ]
        )


def test_browser_transport_input_loader_requires_owner_private_projection_key(
    tmp_path: Path,
) -> None:
    from scripts.a8_run_physical_gate import _load_browser_transport_inputs

    browser = tmp_path / "browser.json"
    transport = tmp_path / "transport.json"
    authority = tmp_path / "transport-authority.json"
    browser_authority = tmp_path / "browser-authority.json"
    key_file = tmp_path / "projection.key"
    browser.write_text('{"protocol":"browser"}', encoding="utf-8")
    transport.write_text('{"protocol":"transport"}', encoding="utf-8")
    authority.write_text('{"protocol":"authority"}', encoding="utf-8")
    authority.chmod(0o600)
    browser_authority.write_text('{"protocol":"browser-authority"}', encoding="utf-8")
    browser_authority.chmod(0o600)
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o644)
    with pytest.raises(PhysicalGateError) as exc_info:
        _load_browser_transport_inputs(
            browser_report_file=browser,
            transport_report_files=[transport],
            transport_authority_file=authority,
            browser_authority_file=browser_authority,
            relay_projection_key_file=key_file,
            require_projection_key=True,
        )
    assert exc_info.value.code == "relay_projection_key_invalid"

    key_file.chmod(0o600)
    inputs = _load_browser_transport_inputs(
        browser_report_file=browser,
        transport_report_files=[transport],
        transport_authority_file=authority,
        browser_authority_file=browser_authority,
        relay_projection_key_file=key_file,
        require_projection_key=True,
    )
    assert inputs["browser_report"] == {"protocol": "browser"}
    assert inputs["transport_reports"] == [{"protocol": "transport"}]
    assert inputs["browser_authority"] == {"protocol": "browser-authority"}
    assert inputs["relay_projection_key"] == b"k" * 32


def test_browser_transport_input_loader_requires_owner_private_authority(
    tmp_path: Path,
) -> None:
    from scripts.a8_run_physical_gate import _load_browser_transport_inputs

    browser = tmp_path / "browser.json"
    transport = tmp_path / "transport.json"
    authority = tmp_path / "transport-authority.json"
    browser_authority = tmp_path / "browser-authority.json"
    browser.write_text('{"protocol":"browser"}', encoding="utf-8")
    transport.write_text('{"protocol":"transport"}', encoding="utf-8")
    authority.write_text('{"protocol":"authority"}', encoding="utf-8")
    authority.chmod(0o644)
    browser_authority.write_text('{"protocol":"browser-authority"}', encoding="utf-8")
    browser_authority.chmod(0o600)

    with pytest.raises(PhysicalGateError) as exc_info:
        _load_browser_transport_inputs(
            browser_report_file=browser,
            transport_report_files=[transport],
            transport_authority_file=authority,
            browser_authority_file=browser_authority,
            relay_projection_key_file=None,
            require_projection_key=False,
        )
    assert exc_info.value.code == "transport_observation_signature_invalid"

    authority.chmod(0o600)
    inputs = _load_browser_transport_inputs(
        browser_report_file=browser,
        transport_report_files=[transport],
        transport_authority_file=authority,
        browser_authority_file=browser_authority,
        relay_projection_key_file=None,
        require_projection_key=False,
    )
    assert inputs["transport_authority"] == {"protocol": "authority"}


def test_browser_transport_input_loader_requires_owner_private_browser_authority(
    tmp_path: Path,
) -> None:
    from scripts.a8_run_physical_gate import _load_browser_transport_inputs

    browser = tmp_path / "browser.json"
    transport = tmp_path / "transport.json"
    transport_authority = tmp_path / "transport-authority.json"
    browser_authority = tmp_path / "browser-authority.json"
    browser.write_text('{"protocol":"browser"}', encoding="utf-8")
    transport.write_text('{"protocol":"transport"}', encoding="utf-8")
    transport_authority.write_text(
        '{"protocol":"transport-authority"}', encoding="utf-8"
    )
    transport_authority.chmod(0o600)
    browser_authority.write_text('{"protocol":"browser-authority"}', encoding="utf-8")
    browser_authority.chmod(0o644)

    with pytest.raises(PhysicalGateError) as exc_info:
        _load_browser_transport_inputs(
            browser_report_file=browser,
            transport_report_files=[transport],
            transport_authority_file=transport_authority,
            browser_authority_file=browser_authority,
            relay_projection_key_file=None,
            require_projection_key=False,
        )
    assert exc_info.value.code == "browser_observation_signature_invalid"


def test_preflight_dry_run_is_inert_and_claims_nothing() -> None:
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=SOURCE_DIGEST,
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is False
    assert document["result"] == "not_executed"
    assert document["evidence_digests"] == []
    assert document["projection_digest"] is None
    assert document["protocol"] == INTERNET_NATIVE_QUALIFICATION_PROTOCOL


def test_cli_builds_owner_private_transport_authority_from_endpoint_keys(
    tmp_path: Path,
) -> None:
    identity = tmp_path / "identity"
    identity.mkdir(mode=0o700)
    endpoint_key = identity / "endpoint.key"
    private_bytes = bytes(range(32))
    endpoint_key.write_bytes(private_bytes)
    endpoint_key.chmod(0o600)
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    authority_file = authority_root / "transport-authority.json"
    command = [
        "/opt/homebrew/bin/python3.14",
        str(RUNNER),
        "build-transport-authority",
        "--deployment-id",
        "deployment-a8",
        "--endpoint-secret-file",
        str(endpoint_key),
        "--output-file",
        str(authority_file),
    ]

    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    document = json.loads(authority_file.read_text("utf-8"))
    public_bytes = (
        Ed25519PrivateKey.from_private_bytes(private_bytes)
        .public_key()
        .public_bytes_raw()
    )
    assert document == {
        "protocol": "mycelium.a8_transport_authority.v1",
        "deployment_id": "deployment-a8",
        "endpoints": [
            {
                "endpoint_id": public_bytes.hex(),
                "verification_key_digest": "sha256:"
                + hashlib.sha256(public_bytes).hexdigest(),
            }
        ],
    }
    assert authority_file.stat().st_mode & 0o777 == 0o600

    repeated = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert repeated.returncode == 2
    assert json.loads(authority_file.read_text("utf-8")) == document


def test_cli_builds_owner_private_browser_authority_from_collector_key(
    tmp_path: Path,
) -> None:
    identity = tmp_path / "identity"
    identity.mkdir(mode=0o700)
    signing_key = identity / "browser.key"
    private_bytes = bytes(reversed(range(32)))
    signing_key.write_bytes(private_bytes)
    signing_key.chmod(0o600)
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    authority_file = authority_root / "browser-authority.json"
    command = [
        "/opt/homebrew/bin/python3.14",
        str(RUNNER),
        "build-browser-authority",
        "--signing-key-file",
        str(signing_key),
        "--case-id",
        "direct_path_qualified_browser_inference",
        "--origin",
        "https://a8.example.test",
        "--deployment-id",
        "deployment-a8",
        "--spec-digest",
        PLACEHOLDER,
        "--source-digest",
        SOURCE_DIGEST,
        "--request-count",
        "1",
        "--now-unix-ms",
        "1752500000000",
        "--output-file",
        str(authority_file),
    ]

    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    public_bytes = (
        Ed25519PrivateKey.from_private_bytes(private_bytes)
        .public_key()
        .public_bytes_raw()
    )
    authority = json.loads(authority_file.read_text("utf-8"))
    challenge_id = authority.pop("challenge_id")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", challenge_id)
    assert challenge_id != "sha256:" + "0" * 64
    assert authority == {
        "protocol": "mycelium.a8_browser_observation_authority.v2",
        "signer_id": "a8-browser-collector",
        "case_id": "direct_path_qualified_browser_inference",
        "origin": "https://a8.example.test",
        "deployment_id": "deployment-a8",
        "spec_digest": PLACEHOLDER,
        "source_digest": SOURCE_DIGEST,
        "request_count": 1,
        "issued_at_unix_ms": 1_752_500_000_000,
        "expires_at_unix_ms": 1_752_500_300_000,
        "verification_keys": [
            {
                "algorithm": "ed25519",
                "encoding": "base64",
                "verification_key": base64.b64encode(public_bytes).decode("ascii"),
                "verification_key_digest": "sha256:"
                + hashlib.sha256(public_bytes).hexdigest(),
            }
        ],
    }
    assert authority_file.stat().st_mode & 0o777 == 0o600


def test_owner_private_authority_writer_rejects_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    from scripts.a8_run_physical_gate import _write_owner_private_json

    real_root = tmp_path / "real-root"
    authority_root = real_root / "authority"
    authority_root.mkdir(parents=True, mode=0o700)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(OSError):
        _write_owner_private_json(
            linked_root / "authority" / "authority.json",
            {"protocol": "test.authority.v1"},
        )
    assert list(authority_root.iterdir()) == []


def test_execution_and_sealing_reject_stale_source_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mycelium_internet.physical as physical

    source_root = tmp_path / "source"
    source_root.mkdir()
    pinned = source_root / "candidate.py"
    pinned.write_bytes(b"candidate-v1\n")
    manifest = {
        "protocol": "mycelium.a8_source_manifest.v1",
        "base_commit": "a" * 40,
        "files": [
            {
                "path": "candidate.py",
                "sha256": "sha256:" + hashlib.sha256(pinned.read_bytes()).hexdigest(),
                "size_bytes": pinned.stat().st_size,
            }
        ],
    }
    manifest_file = source_root / "manifest.json"
    manifest_file.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    source_digest = "sha256:" + hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    monkeypatch.setattr(physical, "_DEFAULT_SOURCE_ROOT", source_root)
    monkeypatch.setattr(physical, "_DEFAULT_SOURCE_MANIFEST", manifest_file)

    document = execute_case(
        "missing_or_stale_path_measurements",
        origin="https://seed.example.invalid",
        evidence_root=None,
        spec_digest=PLACEHOLDER,
        source_digest=source_digest,
    )
    assert document["result"] == "passed"

    pinned.write_bytes(b"candidate-v2\n")
    with pytest.raises(PhysicalGateError) as execute_error:
        execute_case(
            "missing_or_stale_path_measurements",
            origin="https://seed.example.invalid",
            evidence_root=None,
            spec_digest=PLACEHOLDER,
            source_digest=source_digest,
        )
    assert execute_error.value.code == "source_binding_invalid"

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    with pytest.raises(PhysicalGateError) as seal_error:
        seal_qualification(document, evidence_root=evidence_root)
    assert seal_error.value.code == "source_binding_invalid"
    assert list(evidence_root.iterdir()) == []


def test_execution_and_sealing_reject_placeholder_source_bindings(
    tmp_path: Path,
) -> None:
    zero = "sha256:" + "0" * 64
    with pytest.raises(PhysicalGateError) as execute_error:
        execute_case(
            "missing_or_stale_path_measurements",
            origin="https://seed.example.invalid",
            evidence_root=None,
            spec_digest=zero,
            source_digest=SOURCE_DIGEST,
        )
    assert execute_error.value.code == "source_binding_invalid"

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=zero,
    )
    document["executed"] = True
    document["result"] = "passed"
    document["evidence_digests"] = [PLACEHOLDER]
    document["projection_digest"] = PLACEHOLDER
    with pytest.raises(PhysicalGateError) as seal_error:
        seal_qualification(document, evidence_root=evidence_root)
    assert seal_error.value.code == "source_binding_invalid"
    assert list(evidence_root.iterdir()) == []


def test_cli_preflight_exits_zero_with_inert_envelope(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            "/opt/homebrew/bin/python3.14",
            str(RUNNER),
            "preflight",
            "--spec-digest",
            PLACEHOLDER,
            "--source-digest",
            PLACEHOLDER,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    validate_internet_native_qualification(document)
    assert document["executed"] is False


def test_run_without_reachable_origin_fails_closed_and_writes_nothing(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    with pytest.raises(PhysicalGateError) as exc_info:
        execute_case(
            "cleartext_or_redirect_bootstrap",
            origin="https://seed.example.invalid",
            evidence_root=evidence_root,
            spec_digest=PLACEHOLDER,
            source_digest=SOURCE_DIGEST,
            adapter=None,
        )
    assert exc_info.value.code == "physical_infrastructure_unavailable"
    assert list(evidence_root.iterdir()) == []


def test_peer_required_cases_fail_closed_without_a_peer(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    for case_id in PEER_REQUIRED_CASES:
        with pytest.raises(PeerRequired) as exc_info:
            execute_case(
                case_id,
                origin="https://seed.example.invalid",
                evidence_root=evidence_root,
                spec_digest=PLACEHOLDER,
                source_digest=SOURCE_DIGEST,
                adapter=None,
            )
        assert exc_info.value.code == "peer_required"
    assert list(evidence_root.iterdir()) == []


def test_unknown_case_id_is_rejected() -> None:
    with pytest.raises(PhysicalGateError) as exc_info:
        execute_case(
            "not_a_real_case",
            origin="https://seed.example.invalid",
            evidence_root=None,
            spec_digest=PLACEHOLDER,
            source_digest=SOURCE_DIGEST,
            adapter=None,
        )
    assert exc_info.value.code == "case_unknown"


def test_seal_resists_evidence_root_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mycelium_internet.physical as physical

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    anchored_root = tmp_path / "anchored-evidence"
    attacker_root = tmp_path / "attacker"
    attacker_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=SOURCE_DIGEST,
    )
    document["executed"] = True
    document["result"] = "passed"
    document["evidence_digests"] = [PLACEHOLDER]
    document["projection_digest"] = PLACEHOLDER
    monkeypatch.setattr(
        physical,
        "_verify_default_source_binding",
        lambda value: str(value),
    )
    real_open = physical.os.open
    target = evidence_root / f"qualification-{document['qualification_id']}.json"
    swapped = False

    def swapping_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if not swapped and Path(path) == target and kwargs.get("dir_fd") is None:
            evidence_root.rename(anchored_root)
            evidence_root.symlink_to(attacker_root, target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(physical.os, "open", swapping_open)
    seal_qualification(document, evidence_root=evidence_root)

    assert list(attacker_root.iterdir()) == []
    if swapped:
        assert list(anchored_root.iterdir())
    else:
        assert list(evidence_root.iterdir())


def test_private_input_reader_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    from scripts.a8_run_physical_gate import _read_descriptor_bytes

    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    key = actual / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(OSError):
        _read_descriptor_bytes(
            linked / "key",
            maximum_size=32,
            owner_private=True,
        )


def test_seal_rejects_record_name_inode_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mycelium_internet.physical as physical

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=SOURCE_DIGEST,
    )
    document["executed"] = True
    document["result"] = "passed"
    document["evidence_digests"] = [PLACEHOLDER]
    document["projection_digest"] = PLACEHOLDER
    monkeypatch.setattr(
        physical,
        "_verify_default_source_binding",
        lambda value: str(value),
    )
    target = evidence_root / f"qualification-{document['qualification_id']}.json"
    real_fsync = physical.os.fsync
    calls = 0

    def swapping_fsync(descriptor: int) -> None:
        nonlocal calls
        real_fsync(descriptor)
        calls += 1
        if calls == 1:
            target.unlink()
            target.write_bytes(b"attacker")
            target.chmod(0o400)

    monkeypatch.setattr(physical.os, "fsync", swapping_fsync)
    with pytest.raises(PhysicalGateError) as exc_info:
        seal_qualification(document, evidence_root=evidence_root)
    assert exc_info.value.code == "evidence_root_unsafe"
    assert not target.exists()


def test_seal_rechecks_source_binding_after_record_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mycelium_internet.physical as physical

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=SOURCE_DIGEST,
    )
    document["executed"] = True
    document["result"] = "passed"
    document["evidence_digests"] = [PLACEHOLDER]
    document["projection_digest"] = PLACEHOLDER
    calls = 0

    def source_check(value: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PhysicalGateError("source_binding_invalid")
        return str(value)

    monkeypatch.setattr(physical, "_verify_default_source_binding", source_check)
    with pytest.raises(PhysicalGateError) as exc_info:
        seal_qualification(document, evidence_root=evidence_root)
    assert exc_info.value.code == "source_binding_invalid"
    assert list(evidence_root.iterdir()) == []


def test_seal_writes_locked_owner_private_record(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=SOURCE_DIGEST,
    )
    document["executed"] = True
    document["result"] = "passed"
    document["evidence_digests"] = [PLACEHOLDER]
    document["projection_digest"] = PLACEHOLDER
    validate_internet_native_qualification(document)
    record = seal_qualification(
        document,
        evidence_root=evidence_root,
    )
    assert record.exists()
    assert record.read_text("utf-8").startswith("{")
    assert record.stat().st_mode & 0o777 == 0o400
    assert evidence_root.stat().st_mode & 0o022 == 0


def test_seal_rejects_failed_qualification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mycelium_internet.physical as physical

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=SOURCE_DIGEST,
    )
    document["executed"] = True
    document["result"] = "failed"
    document["evidence_digests"] = [PLACEHOLDER]
    document["projection_digest"] = PLACEHOLDER
    validate_internet_native_qualification(document)
    monkeypatch.setattr(
        physical,
        "_verify_default_source_binding",
        lambda value: str(value),
    )

    with pytest.raises(PhysicalGateError) as exc_info:
        seal_qualification(document, evidence_root=evidence_root)

    assert exc_info.value.code == "qualification_not_passed"
    assert list(evidence_root.iterdir()) == []


def test_seal_validates_detached_mapping_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mycelium_internet.physical as physical

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    valid = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=SOURCE_DIGEST,
    )
    valid["executed"] = True
    valid["result"] = "passed"
    valid["evidence_digests"] = [PLACEHOLDER]
    valid["projection_digest"] = PLACEHOLDER

    class SwitchingMapping(Mapping[str, Any]):
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self) -> Iterator[str]:
            self.iterations += 1
            return iter(valid)

        def __len__(self) -> int:
            return len(valid)

        def __getitem__(self, key: str) -> Any:
            if self.iterations >= 3 and key == "result":
                return "attacker_overclaim_not_in_contract"
            return valid[key]

    monkeypatch.setattr(
        physical,
        "_verify_default_source_binding",
        lambda value: str(value),
    )
    record = seal_qualification(SwitchingMapping(), evidence_root=evidence_root)

    sealed = json.loads(record.read_text("utf-8"))
    validate_internet_native_qualification(sealed)
    assert sealed["result"] == "passed"


def test_cli_failed_case_returns_nonzero_and_never_seals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.a8_run_physical_gate as runner

    failed = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=SOURCE_DIGEST,
    )
    failed["executed"] = True
    failed["result"] = "failed"
    failed["evidence_digests"] = [PLACEHOLDER]
    failed["projection_digest"] = PLACEHOLDER
    seal_calls = 0

    monkeypatch.setattr(runner, "execute_case", lambda *_args, **_kwargs: failed)

    def unexpected_seal(*_args: Any, **_kwargs: Any) -> Path:
        nonlocal seal_calls
        seal_calls += 1
        raise AssertionError("failed case must not seal")

    monkeypatch.setattr(runner, "seal_qualification", unexpected_seal)
    result = runner.main(
        [
            "run",
            "missing_or_stale_path_measurements",
            "--origin",
            "https://seed.example.invalid",
            "--spec-digest",
            PLACEHOLDER,
            "--source-digest",
            SOURCE_DIGEST,
            "--evidence-root",
            str(tmp_path),
            "--seal",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "gate failed: case result is not passed" in captured.err
    assert seal_calls == 0


def test_seal_rejects_tampered_digest_form_with_negative_control(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=SOURCE_DIGEST,
    )
    document["executed"] = True
    document["result"] = "passed"
    document["evidence_digests"] = [PLACEHOLDER]
    document["projection_digest"] = PLACEHOLDER
    # Negative control: the un-tampered record still validates.
    validate_internet_native_qualification(document)
    tampered = json.loads(json.dumps(document))
    tampered["projection_digest"] = "sha256:" + "b" * 63
    assert tampered["projection_digest"] != document["projection_digest"]
    with pytest.raises(ValueError):
        validate_internet_native_qualification(tampered)
    with pytest.raises(ValueError):
        seal_qualification(tampered, evidence_root=evidence_root)
    assert list(evidence_root.iterdir()) == []


def test_seal_rejects_not_executed_passed_record(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    document = preflight_document(
        now_unix_ms=1_752_500_000_000,
        spec_digest=PLACEHOLDER,
        source_digest=SOURCE_DIGEST,
    )
    document["result"] = "passed"
    with pytest.raises(ValueError):
        seal_qualification(document, evidence_root=evidence_root)
    assert list(evidence_root.iterdir()) == []


def test_authority_probe_program_executes_and_binds_member(tmp_path: Path) -> None:
    from scripts.a8_run_physical_gate import _authority_probe_via

    program = tmp_path / "probe.py"
    program.write_text(
        """#!/usr/bin/env python3
import json, sys
print(json.dumps({
    \"protocol\": \"mycelium.unqualified_member_authority_probe.v1\",
    \"member_id\": sys.argv[1],
    \"member_visible\": True,
    \"activation_eligible\": False,
    \"authority_attempts\": {
        \"artifact\": \"rejected\", \"placement\": \"rejected\",
        \"activation\": \"rejected\", \"selection\": \"rejected\",
        \"inference\": \"rejected\"
    },
    \"forbidden_side_effects\": {
        \"artifact_disclosed\": False, \"placement_created\": False,
        \"deployment_selected\": False
    },
    \"prompt_deliveries\": 0
}))
""",
        encoding="utf-8",
    )
    program.chmod(0o700)

    probe = _authority_probe_via(program, "peer-node-unqualified")
    report = probe()

    assert report["member_id"] == "peer-node-unqualified"
    assert report["prompt_deliveries"] == 0


def test_case_probe_program_brackets_revocation_and_retains_private_reports(
    tmp_path: Path,
) -> None:
    from scripts.a8_run_physical_gate import _case_probe_via

    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    before_file = private_root / "revocation-probe.before.json"
    after_file = private_root / "revocation-probe.json"
    program = tmp_path / "probe.py"
    program.write_text(
        """#!/usr/bin/env python3
import json, sys
phase = sys.argv[3]
previous = json.load(sys.stdin) if phase == "after" else None
print(json.dumps({
    "case_id": sys.argv[1],
    "member_id": sys.argv[2],
    "phase": phase,
    "previous_member_id": None if previous is None else previous["member_id"]
}))
""",
        encoding="utf-8",
    )
    program.chmod(0o700)

    before_probe = _case_probe_via(
        program,
        "revoked_active_member",
        "peer-node-revoked",
        before_file,
        phase="before",
    )
    before_report = before_probe()
    after_probe = _case_probe_via(
        program,
        "revoked_active_member",
        "peer-node-revoked",
        after_file,
        phase="after",
    )
    after_report = after_probe(before_report)

    assert before_report["phase"] == "before"
    assert before_report["previous_member_id"] is None
    assert after_report["phase"] == "after"
    assert after_report["previous_member_id"] == "peer-node-revoked"
    assert json.loads(before_file.read_text("utf-8")) == before_report
    assert json.loads(after_file.read_text("utf-8")) == after_report
    assert before_file.stat().st_mode & 0o777 == 0o600
    assert after_file.stat().st_mode & 0o777 == 0o600


def test_revocation_case_probe_inputs_use_before_and_after_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.a8_run_physical_gate as runner

    calls: list[tuple[str, Path]] = []

    def fake_probe(
        _program: Path,
        _case_id: str,
        _member_id: str,
        output_file: Path,
        *,
        phase: str = "observe",
    ):
        calls.append((phase, output_file))
        return lambda _before=None: {"phase": phase}

    monkeypatch.setattr(runner, "_case_probe_via", fake_probe)
    output_file = tmp_path / "revocation-probe.json"
    case_inputs = runner._case_probe_inputs(
        tmp_path / "probe.py",
        "revoked_active_member",
        "peer-node-revoked",
        output_file,
    )

    assert set(case_inputs) == {"case_probe_before", "case_probe_after"}
    assert calls == [
        ("before", tmp_path / "revocation-probe.before.json"),
        ("after", output_file),
    ]


def test_peer_membership_identity_matches_native_iroh_endpoint_secret(
    tmp_path: Path,
) -> None:
    from scripts.a8_run_physical_gate import _peer_node_and_join

    class Adapter:
        _bundle_payload = {"swarm_id": "swarm-a8", "nonce": "invite-a8"}

        def preflight(self, *, now: float) -> dict[str, str]:
            assert now > 0
            return {"seed_node_id": "seed-a8"}

    node_root = tmp_path / "native-peer"
    node, join_envelope = _peer_node_and_join(
        Adapter(), "https://a8.example", node_root
    )
    public_key = base64.b64decode(
        node.signer.public_key_record()["verification_key"], validate=True
    )
    expected_endpoint_id = public_key.hex()

    assert node.signer.endpoint_id == expected_endpoint_id
    assert join_envelope["message"]["sender_endpoint_id"] == expected_endpoint_id
    assert join_envelope["message"]["endpoint_addr"]["id"] == expected_endpoint_id
    assert (node_root / "node.key").stat().st_mode & 0o777 == 0o600
    assert len((node_root / "node.key").read_bytes()) == 32


def test_cli_run_without_infra_exits_nonzero_and_writes_nothing(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    completed = subprocess.run(
        [
            "/opt/homebrew/bin/python3.14",
            str(RUNNER),
            "run",
            "cleartext_or_redirect_bootstrap",
            "--origin",
            "https://seed.example.invalid",
            "--spec-digest",
            PLACEHOLDER,
            "--source-digest",
            SOURCE_DIGEST,
            "--evidence-root",
            str(evidence_root),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode != 0
    assert "physical_infrastructure_unavailable" in completed.stderr
    assert list(evidence_root.iterdir()) == []
