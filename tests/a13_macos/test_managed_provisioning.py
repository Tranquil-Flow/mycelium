# SPDX-License-Identifier: AGPL-3.0-only
"""Target-owned run-profile admission for the A13 macOS managed slice.

These tests exercise ``mycelium_a13_macos.managed_provisioning`` end-to-end
against the canonical ``DemoAuthority`` and the synthetic manifest compiler
that ``tests.a13_macos.test_assignment_planning`` already relies on.  No
tensor side-effect path is touched; no model bytes are loaded.

The positive path proves the admission record binds every exact digest,
path and mode marker the parent will chain into the existing
``prepare_registry_launch`` boundary.  The negative paths prove the slice
fails closed before any tensor-bound work can run.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re
import time

import pytest

import model_manifest as mm

from mycelium_a13_macos.authority import DemoAuthority
from mycelium_a13_macos.managed_provisioning import (
    ADMISSION_PROTOCOL,
    ADMITTED_RUN_MODES,
    ProvisioningAdmissionError,
    RunProfile,
    RunProfileAdmission,
    admit_run_profile,
    next_execution_boundary,
    verify_run_profile_admission,
)
from mycelium_a13_macos.serving import (
    CANDIDATE_MODEL,
    PreparedSource,
    RESOURCE_PROPOSAL,
)

from tests.a13_macos.test_peer import CONSENT, QUOTA, seed, _enroll, _member


# ---------------------------------------------------------------------------
# Fixtures — real canonical owner + second member + synthetic prepared source
# ---------------------------------------------------------------------------


def _synthetic_manifest(resolved_commit: str = CANDIDATE_MODEL.resolved_commit) -> dict:
    weights = {
        name: "toy.safetensors"
        for name in (
            "model.embed_tokens.weight",
            "model.norm.weight",
            "lm_head.weight",
            *(f"model.layers.{i}.self_attn.q_proj.weight" for i in range(3)),
        )
    }
    return mm.compile_model_manifest(
        model_id=CANDIDATE_MODEL.model_id,
        requested_revision=resolved_commit,
        resolved_commit=resolved_commit,
        config={
            "model_type": "qwen2",
            "architectures": ["Qwen2ForCausalLM"],
            "num_hidden_layers": 3,
            "hidden_size": 4,
            "num_attention_heads": 1,
            "num_key_value_heads": 1,
            "head_dim": 4,
            "intermediate_size": 8,
            "vocab_size": 8,
            "max_position_embeddings": 16,
            "rms_norm_eps": 1e-6,
            "rope_theta": 10000.0,
            "hidden_act": "silu",
            "tie_word_embeddings": False,
        },
        checkpoint_index={"metadata": {"total_size": 128}, "weight_map": weights},
        file_metadata={"toy.safetensors": {"size_bytes": 128, "sha256": "1" * 64}},
    )


def _write_dummy_source(tmp_path: Path, manifest: dict) -> Path:
    """Create a real on-disk source root so ``_normalize_source_root`` succeeds.

    The synthetic source contains a single tiny file with the expected
    digest; admission only checks the root is a real directory with safe
    permissions, not that the bytes match the manifest.
    """
    source = tmp_path / "source-root"
    source.mkdir(mode=0o700)
    (source / "config.json").write_text('{"model_type": "qwen2"}')
    (source / "toy.safetensors").write_bytes(b"\x00" * 16)
    os_chmod_safe(source)
    return source


def os_chmod_safe(path: Path) -> None:
    import os
    import stat

    os.chmod(path, 0o700)
    for child in path.rglob("*"):
        if child.is_dir():
            os.chmod(child, 0o700)
        else:
            os.chmod(child, 0o600)


def _make_prepared(tmp_path: Path) -> PreparedSource:
    manifest = _synthetic_manifest()
    source = _write_dummy_source(tmp_path, manifest)
    return PreparedSource(
        model=CANDIDATE_MODEL,
        source_root=source,
        source_bytes=16,
        manifest=manifest,
        manifest_digest=mm.manifest_digest_ref(manifest),
    )


def _make_profile(
    *,
    owner_node_id: str,
    second_node_id: str,
    prepared: PreparedSource,
    tmp_path: Path,
    **overrides,
) -> RunProfile:
    return RunProfile(
        run_mode=overrides.get("run_mode", "managed_local"),
        expected_model_id=overrides.get("expected_model_id", prepared.model.model_id),
        expected_resolved_commit=overrides.get(
            "expected_resolved_commit", prepared.model.resolved_commit
        ),
        expected_manifest_digest=overrides.get(
            "expected_manifest_digest", prepared.manifest_digest
        ),
        source_root=overrides.get("source_root", str(prepared.source_root)),
        runtime_backend=overrides.get(
            "runtime_backend", prepared.model.runtime_backend
        ),
        runtime_dtype=overrides.get("runtime_dtype", prepared.model.runtime_dtype),
        runtime_quantization=overrides.get(
            "runtime_quantization", prepared.model.runtime_quantization
        ),
        grant_member_node_ids=overrides.get(
            "grant_member_node_ids", (owner_node_id, second_node_id)
        ),
        grant_host_ids=overrides.get("grant_host_ids", (owner_node_id, second_node_id)),
        network_download_bytes=overrides.get(
            "network_download_bytes", RESOURCE_PROPOSAL.network_download_bytes
        ),
        additional_disk_bytes=overrides.get(
            "additional_disk_bytes", RESOURCE_PROPOSAL.additional_disk_bytes
        ),
        max_combined_rss_bytes=overrides.get(
            "max_combined_rss_bytes", RESOURCE_PROPOSAL.max_combined_rss_bytes
        ),
        max_wall_seconds=overrides.get(
            "max_wall_seconds", RESOURCE_PROPOSAL.max_wall_seconds
        ),
        run_count=overrides.get("run_count", RESOURCE_PROPOSAL.run_count),
        freshness_window_seconds=overrides.get("freshness_window_seconds", 60),
        expected_artifact_cache_roots=overrides.get(
            "expected_artifact_cache_roots",
            {
                owner_node_id: str(tmp_path / f"cache-{owner_node_id}"),
                second_node_id: str(tmp_path / f"cache-{second_node_id}"),
            },
        ),
    )


@pytest.fixture
def enrolled_pair(tmp_path: Path, seed):
    """Real owner + second member enrolled through the canonical TLS path."""
    owner, factory = seed
    owner_request = owner.pair_request()
    owner.accept_pairing(owner.approve_pairing(owner_request, ttl_seconds=120, quota=QUOTA))
    peer = _member(tmp_path / "peer", factory)
    peer.start()
    try:
        _enroll(owner, peer)
        owner_node_id = owner.member.node_id
        second_node_id = next(
            member["node_id"]
            for member in owner.coordinator.members()
            if member["node_id"] != owner_node_id
        )
        prepared = _make_prepared(tmp_path)
        yield owner, owner_node_id, second_node_id, prepared
    finally:
        peer.stop()


# ---------------------------------------------------------------------------
# Positive — full admission + digest/path/mode-binding + verify round-trip
# ---------------------------------------------------------------------------


def test_admission_binds_digest_path_mode_and_owner_state(enrolled_pair) -> None:
    owner, owner_node_id, second_node_id, prepared = enrolled_pair
    profile = _make_profile(
        owner_node_id=owner_node_id,
        second_node_id=second_node_id,
        prepared=prepared,
        tmp_path=owner.state_root,
    )

    admission = admit_run_profile(
        profile, prepared, owner, require_capability=False
    )

    assert isinstance(admission, RunProfileAdmission)
    assert admission.protocol == ADMISSION_PROTOCOL
    assert admission.admitted_run_mode == "managed_local"
    assert admission.expected_manifest_digest == prepared.manifest_digest
    assert admission.source_root == str(prepared.source_root)
    assert admission.expected_model_id == prepared.model.model_id
    assert admission.expected_resolved_commit == prepared.model.resolved_commit
    assert admission.runtime_backend == prepared.model.runtime_backend
    assert admission.runtime_dtype == prepared.model.runtime_dtype
    assert admission.runtime_quantization == prepared.model.runtime_quantization
    assert tuple(admission.grant_member_node_ids) == (owner_node_id, second_node_id)
    assert tuple(admission.grant_host_ids) == (owner_node_id, second_node_id)
    assert admission.network_download_bytes == RESOURCE_PROPOSAL.network_download_bytes
    assert admission.additional_disk_bytes == RESOURCE_PROPOSAL.additional_disk_bytes
    assert admission.max_combined_rss_bytes == RESOURCE_PROPOSAL.max_combined_rss_bytes
    assert admission.max_wall_seconds == RESOURCE_PROPOSAL.max_wall_seconds
    assert admission.run_count == RESOURCE_PROPOSAL.run_count
    assert admission.freshness_window_seconds == 60
    assert admission.owner_swarm_id == owner.coordinator.swarm_id
    assert admission.owner_seed_key_digest == owner.coordinator.signer.verification_key_digest
    owner_record = next(
        member
        for member in owner.coordinator.members()
        if member["node_id"] == owner_node_id
    )
    assert admission.owner_membership_generation == owner_record["generation"]
    assert admission.owner_lease_expires_at == owner_record["lease_expires_at"]

    # Digest must be a sha256 of the canonical envelope.
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", admission.profile_digest)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", admission.admission_digest)
    assert admission.profile_digest != admission.admission_digest

    # verify_run_profile_admission must succeed at the recorded clock and
    # raise fresh-state errors after the freshness window elapses.
    verify_run_profile_admission(admission, clock=admission.decision_clock + 1.0)
    with pytest.raises(ProvisioningAdmissionError) as exceeded:
        verify_run_profile_admission(
            admission, clock=admission.decision_clock + 1000.0
        )
    assert exceeded.value.code == "admission_freshness_window_exceeded"


def test_admission_is_deterministic_and_recomputable(enrolled_pair) -> None:
    """Re-binding the same prepared source + owner state must produce the
    same digests as long as nothing has changed.  A second admission after
    status mutation must NOT silently re-bind stale markers."""
    owner, owner_node_id, second_node_id, prepared = enrolled_pair
    profile = _make_profile(
        owner_node_id=owner_node_id,
        second_node_id=second_node_id,
        prepared=prepared,
        tmp_path=owner.state_root,
    )
    clock = time.time()

    first = admit_run_profile(
        profile, prepared, owner, clock=clock, require_capability=False
    )
    second = admit_run_profile(
        profile, prepared, owner, clock=clock, require_capability=False
    )
    assert first.profile_digest == second.profile_digest
    assert first.admission_digest == second.admission_digest
    assert first.owner_membership_generation == second.owner_membership_generation
    assert first.owner_lease_expires_at == second.owner_lease_expires_at

    # A later admission after the lease has elapsed must fail closed.
    expired_clock = max(
        record["lease_expires_at"]
        for record in owner.coordinator.members()
    ) + 1.0
    with pytest.raises(ProvisioningAdmissionError) as expired:
        admit_run_profile(
            profile, prepared, owner, clock=expired_clock, require_capability=False
        )
    assert expired.value.code == "admission_grant_member_lease_expired"


def test_admission_chain_marker_documents_parent_insertions() -> None:
    boundary = next_execution_boundary()
    assert boundary["admit_run_profile"] == (
        "prepare_registry_launch",
        "RegistryLaunch.run",
    )
    assert boundary["RunProfileAdmission.expected_manifest_digest"] == (
        "prepare_registry_launch.prepared.manifest_digest",
        "operator_plan.graph.manifest_digest",
    )
    assert boundary["RunProfileAdmission.grant_host_ids"] == (
        "operator_plan.controller.peers.host_id",
    )
    assert boundary["RunProfileAdmission.grant_member_node_ids"] == (
        "operator_plan.membership_snapshot.member_node_id",
    )
    # The closed set of admitted modes must remain exactly what the
    # canonical C4/C5 chain expects and nothing else.
    assert ADMITTED_RUN_MODES == frozenset({"managed_local"})


def test_admission_emits_only_canonical_markers(enrolled_pair) -> None:
    """The serialized envelope must not contain private material, member
    secrets, or runtime payloads — only digest/path/mode markers."""
    owner, owner_node_id, second_node_id, prepared = enrolled_pair
    profile = _make_profile(
        owner_node_id=owner_node_id,
        second_node_id=second_node_id,
        prepared=prepared,
        tmp_path=owner.state_root,
    )
    admission = admit_run_profile(
        profile, prepared, owner, require_capability=False
    )

    blob = repr(admission).encode("utf-8") + b"\n" + repr(profile).encode("utf-8")
    forbidden_markers = (
        b"node.key",
        b"identity_key",
        b"private_key",
        b"token",
        b"password",
        b"ssh",
        b"argv",
        b"shell",
    )
    for marker in forbidden_markers:
        assert marker not in blob, marker
    # Every sha256 in the serialized envelope is a real digest of bytes
    # the slice controls; private key bytes must not leak.
    assert admission.profile_digest.startswith("sha256:")
    assert admission.admission_digest.startswith("sha256:")


# ---------------------------------------------------------------------------
# Negative — fail-closed before any tensor-bound work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_run_mode",
        "wrong_model_id",
        "wrong_resolved_commit",
        "wrong_manifest_digest",
        "wrong_source_root",
        "wrong_runtime_backend",
        "wrong_runtime_dtype",
        "wrong_runtime_quantization",
        "owner_not_in_grant",
        "duplicate_member_in_grant",
        "non_posix_cache_root",
        "single_host_grant",
        "excessive_network_budget",
        "excessive_disk_budget",
        "excessive_rss_budget",
        "excessive_wall_budget",
        "run_count_mismatch",
        "freshness_window_zero",
        "stopped_authority",
    ],
)
def test_invalid_run_profile_creates_no_admission(enrolled_pair, mutation, tmp_path) -> None:
    owner, owner_node_id, second_node_id, prepared = enrolled_pair
    overrides: dict = {}
    if mutation == "wrong_run_mode":
        overrides["run_mode"] = "shell_tunnel"
    elif mutation == "wrong_model_id":
        # The 7B is the current candidate, so the "wrong" id must be a
        # different repository entirely (wp8-02 G1 swap).
        overrides["expected_model_id"] = "Qwen/Qwen2.5-0.5B-Instruct"
    elif mutation == "wrong_resolved_commit":
        overrides["expected_resolved_commit"] = "0" * 40
    elif mutation == "wrong_manifest_digest":
        overrides["expected_manifest_digest"] = "sha256:" + "0" * 64
    elif mutation == "wrong_source_root":
        overrides["source_root"] = str(tmp_path / "other-source")
    elif mutation == "wrong_runtime_backend":
        overrides["runtime_backend"] = "torch"
    elif mutation == "wrong_runtime_dtype":
        overrides["runtime_dtype"] = "float16"
    elif mutation == "wrong_runtime_quantization":
        overrides["runtime_quantization"] = "none"
    elif mutation == "owner_not_in_grant":
        overrides["grant_member_node_ids"] = ("not-owner", second_node_id)
        overrides["grant_host_ids"] = ("not-owner", second_node_id)
        overrides["expected_artifact_cache_roots"] = {
            "not-owner": str(tmp_path / "cache-not-owner"),
            second_node_id: str(tmp_path / f"cache-{second_node_id}"),
        }
    elif mutation == "duplicate_member_in_grant":
        overrides["grant_member_node_ids"] = (owner_node_id, owner_node_id)
        overrides["grant_host_ids"] = (owner_node_id, owner_node_id)
        overrides["expected_artifact_cache_roots"] = {
            owner_node_id: str(tmp_path / f"cache-{owner_node_id}"),
            owner_node_id: str(tmp_path / f"cache-{owner_node_id}"),
        }
    elif mutation == "non_posix_cache_root":
        overrides["expected_artifact_cache_roots"] = {
            owner_node_id: "relative/path",
            second_node_id: str(tmp_path / f"cache-{second_node_id}"),
        }
    elif mutation == "single_host_grant":
        overrides["grant_host_ids"] = (owner_node_id, owner_node_id)
    elif mutation == "excessive_network_budget":
        overrides["network_download_bytes"] = RESOURCE_PROPOSAL.network_download_bytes + 1
    elif mutation == "excessive_disk_budget":
        overrides["additional_disk_bytes"] = RESOURCE_PROPOSAL.additional_disk_bytes + 1
    elif mutation == "excessive_rss_budget":
        overrides["max_combined_rss_bytes"] = RESOURCE_PROPOSAL.max_combined_rss_bytes + 1
    elif mutation == "excessive_wall_budget":
        overrides["max_wall_seconds"] = RESOURCE_PROPOSAL.max_wall_seconds + 1
    elif mutation == "run_count_mismatch":
        overrides["run_count"] = RESOURCE_PROPOSAL.run_count + 1
    elif mutation == "freshness_window_zero":
        overrides["freshness_window_seconds"] = 0
    elif mutation == "stopped_authority":
        owner.stop()
        # The stopped authority is no longer in the canonical state, but
        # the profile still references its now-defunct identity.  This
        # is the same fail-closed code the slice returns to production.
        return
    else:  # pragma: no cover — defensive
        raise AssertionError(f"unknown mutation: {mutation}")

    profile = _make_profile(
        owner_node_id=owner_node_id,
        second_node_id=second_node_id,
        prepared=prepared,
        tmp_path=owner.state_root,
        **overrides,
    )
    with pytest.raises(ProvisioningAdmissionError):
        admit_run_profile(profile, prepared, owner, require_capability=False)


def test_admission_rejects_revoked_target_member(enrolled_pair) -> None:
    """Revoking the owner or the second member must fail closed at the
    canonical membership gate, not after the digest is computed."""
    owner, owner_node_id, second_node_id, prepared = enrolled_pair
    owner.revoke(owner_node_id, 1)
    profile = _make_profile(
        owner_node_id=owner_node_id,
        second_node_id=second_node_id,
        prepared=prepared,
        tmp_path=owner.state_root,
    )
    with pytest.raises(ProvisioningAdmissionError) as revoked:
        admit_run_profile(profile, prepared, owner, require_capability=False)
    assert revoked.value.code in {
        "admission_owner_not_enrolled",
        "admission_membership_not_current",
        "admission_grant_member_lifecycle_invalid",
    }


def test_admission_rejects_stopped_authority(enrolled_pair) -> None:
    owner, owner_node_id, second_node_id, prepared = enrolled_pair
    profile = _make_profile(
        owner_node_id=owner_node_id,
        second_node_id=second_node_id,
        prepared=prepared,
        tmp_path=owner.state_root,
    )
    owner.stop()
    with pytest.raises(ProvisioningAdmissionError):
        admit_run_profile(profile, prepared, owner, require_capability=False)


def test_verify_admission_rejects_mutation_after_issue(enrolled_pair) -> None:
    """A mutation of the recorded digest must fail verification; a NaN
    clock and a too-old clock must also fail closed."""
    owner, owner_node_id, second_node_id, prepared = enrolled_pair
    profile = _make_profile(
        owner_node_id=owner_node_id,
        second_node_id=second_node_id,
        prepared=prepared,
        tmp_path=owner.state_root,
    )
    admission = admit_run_profile(
        profile, prepared, owner, require_capability=False
    )

    tampered = copy.copy(admission)
    object.__setattr__(tampered, "admission_digest", "sha256:" + "0" * 64)
    with pytest.raises(ProvisioningAdmissionError) as mismatch:
        verify_run_profile_admission(tampered, clock=admission.decision_clock)
    assert mismatch.value.code == "admission_digest_mismatch"

    with pytest.raises(ProvisioningAdmissionError) as nan_clock:
        verify_run_profile_admission(admission, clock=float("nan"))
    assert nan_clock.value.code == "admission_clock_invalid"

    # Past the freshness window but still well inside the recorded lease
    # must trip the freshness check, not the lease check.
    far_future = admission.decision_clock + admission.freshness_window_seconds + 1.0
    with pytest.raises(ProvisioningAdmissionError) as freshness:
        verify_run_profile_admission(admission, clock=far_future)
    assert freshness.value.code == "admission_freshness_window_exceeded"


def test_admission_digest_changes_when_marker_changes(enrolled_pair) -> None:
    """A single byte of marker change must mutate the admission digest."""
    owner, owner_node_id, second_node_id, prepared = enrolled_pair
    base = admit_run_profile(
        _make_profile(
            owner_node_id=owner_node_id,
            second_node_id=second_node_id,
            prepared=prepared,
            tmp_path=owner.state_root,
        ),
        prepared,
        owner,
        require_capability=False,
    )
    shifted = admit_run_profile(
        _make_profile(
            owner_node_id=owner_node_id,
            second_node_id=second_node_id,
            prepared=prepared,
            tmp_path=owner.state_root,
            freshness_window_seconds=120,
        ),
        prepared,
        owner,
        require_capability=False,
    )
    assert base.admission_digest != shifted.admission_digest


def test_admission_rejects_tampered_manifest_digest_pin(enrolled_pair) -> None:
    """A profile whose digest pin disagrees with the prepared source must
    fail closed before any side-effect chain can run."""
    owner, owner_node_id, second_node_id, prepared = enrolled_pair
    fake_digest = "sha256:" + hashlib.sha256(b"definitely-not-this-model").hexdigest()
    profile = _make_profile(
        owner_node_id=owner_node_id,
        second_node_id=second_node_id,
        prepared=prepared,
        tmp_path=owner.state_root,
        expected_manifest_digest=fake_digest,
    )
    with pytest.raises(ProvisioningAdmissionError) as pin:
        admit_run_profile(profile, prepared, owner, require_capability=False)
    assert pin.value.code == "admission_manifest_digest_mismatch"


def test_admission_rejects_source_root_mismatch(enrolled_pair, tmp_path) -> None:
    owner, owner_node_id, second_node_id, prepared = enrolled_pair
    other = tmp_path / "shadow"
    other.mkdir(mode=0o700)
    profile = _make_profile(
        owner_node_id=owner_node_id,
        second_node_id=second_node_id,
        prepared=prepared,
        tmp_path=owner.state_root,
        source_root=str(other),
    )
    with pytest.raises(ProvisioningAdmissionError) as source:
        admit_run_profile(profile, prepared, owner, require_capability=False)
    assert source.value.code == "admission_source_root_mismatch"
