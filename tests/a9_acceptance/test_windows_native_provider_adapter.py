# SPDX-License-Identifier: AGPL-3.0-only
"""Native accelerated Windows x86_64 provider adapter — deterministic A9 source tests.

Covers the frozen A9 decisions ``accelerated_windows_backend`` and
``persistent_int8_execution`` (spec §3.2/§3.4) for the source tranche landed in
``mycelium_platform_neutral/windows_provider.py``.

Claim boundary asserted below rather than described: this adapter is *source*.
It is a typed, registry-driven front-end over the landed int8 kernel seam plus a
win32 library-binding seam.  It does not load a provider build on this host, does
not dispatch on Windows hardware, and does not fabricate an acceleration or
qualification claim: the physical acceptance case
``windows_accelerated_physical_positive`` stays unearned until a Windows device
executes it.
"""

from __future__ import annotations

import platform
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from mycelium_platform_neutral.acceleration_profile import (
    PROTOCOL as ACCELERATION_PROTOCOL,
)
from mycelium_platform_neutral.acceleration_profile import AccelerationProfile
from mycelium_platform_neutral.activation_transport import SUPPORTED_DTYPES
from mycelium_platform_neutral.int8_execution import quantized_reference_linear
from mycelium_platform_neutral.primitives import RUNTIME_BACKEND_STAGES
from mycelium_platform_neutral.windows_provider import (
    ACCEPTANCE_COVERAGE,
    ADAPTER_CODES,
    BACKEND_ID,
    CLAIM_BOUNDARY,
    DEVICE_CLASS,
    PLATFORM_FAMILY,
    PROTOCOL,
    SUPPORTED_PROVIDER_FAMILIES,
    WindowsNativeProviderAdapter,
    WindowsProviderUnavailable,
    bind_provider_library,
    detect_windows_dispatch,
    validate_windows_provider_declaration,
    windows_provider_declaration,
)

INVENTORY = Path(__file__).with_name("inventory.v1.json")
SEED = 20260926
NOW = 2_000_000_000.0
PROVIDER_BUILD = b"windows-provider-build\x00"
PROVIDER_DIGEST = "sha256:" + hashlib.sha256(PROVIDER_BUILD).hexdigest()


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _write_library(tmp_path: Path, raw: bytes = PROVIDER_BUILD) -> Path:
    path = tmp_path / "mycelium_windows_provider.dll"
    path.write_bytes(raw)
    return path


def _binding(tmp_path: Path, *, raw: bytes = PROVIDER_BUILD, digest: str | None = None):
    return bind_provider_library(
        library_path=_write_library(tmp_path, raw),
        expected_digest=digest or ("sha256:" + hashlib.sha256(raw).hexdigest()),
        library_loader=lambda location: {"handle": location},
        host_platform="win32",
    )


def _declaration(**overrides):
    values = {
        "provider_family": "directml",
        "build_digest": PROVIDER_DIGEST,
        "driver_runtime_identity": "windows_directml",
        "driver_runtime_version_digest": _digest("b"),
        "activation_precisions": ["float16"],
        "persistent_state_boundary": "warmup",
        "decode_time_full_weight_materializations": 0,
        "per_decode_full_weight_cast": False,
        "stage_local_kv": True,
        "cancellation_supported": True,
        "cleanup_supported": True,
        "workspace_bounded_bytes": 4096,
        "issued_at": NOW,
        "expires_at": NOW + 3600.0,
    }
    values.update(overrides)
    return validate_windows_provider_declaration(values)


def _fixture(tmp_path: Path, out_features: int = 64, in_features: int = 32, rows: int = 16):
    rng = np.random.default_rng(SEED)
    weight = rng.integers(-127, 128, size=(out_features, in_features), dtype=np.int16).astype(
        np.int8
    )
    scales = (rng.random(out_features, dtype=np.float32) * 0.01 + 0.001).astype(np.float32)
    activations = (rng.standard_normal((rows, in_features)) * 3.0).astype(np.float32)
    adapter = WindowsNativeProviderAdapter(
        declaration=_declaration(),
        dispatch=detect_windows_dispatch(system="Windows", machine="AMD64"),
        library=_binding(tmp_path),
        weight=weight,
        scales=scales,
    )
    return adapter, weight, scales, activations


def _code(call):
    with pytest.raises(WindowsProviderUnavailable) as caught:
        call()
    assert caught.value.code in ADAPTER_CODES
    return caught.value.code


def test_declaration_names_the_windows_target_and_claims_no_execution() -> None:
    declaration = windows_provider_declaration()
    assert declaration["protocol"] == PROTOCOL
    assert declaration["platform_family"] == PLATFORM_FAMILY
    assert declaration["device_class"] == DEVICE_CLASS
    assert declaration["claim_level"] == "managed_native_host"
    assert declaration["physical_execution"] == "not_executed"
    assert declaration["claim_boundary"] == CLAIM_BOUNDARY
    assert sorted(declaration["supported_provider_families"]) == sorted(
        SUPPORTED_PROVIDER_FAMILIES
    )
    rendered = repr(declaration).lower()
    assert "qualified" not in rendered
    assert "accelerated': true" not in rendered


def test_backend_identity_is_registered_as_a_runtime_identity_not_a_qualification() -> None:
    assert BACKEND_ID == "windows_native_provider"
    assert RUNTIME_BACKEND_STAGES[BACKEND_ID] == "implemented"
    # Registration is a declared identity; the device blocker is unchanged.
    assert "windows_x86_64_provider_not_physically_qualified" in ADAPTER_CODES
    report = windows_provider_declaration()
    assert report["registry_stage"] == "implemented"
    assert report["registry_entry_is_not_qualification"] is True


def test_dispatch_requires_a_native_windows_x86_64_observation() -> None:
    dispatch = detect_windows_dispatch(system="Windows", machine="AMD64")
    assert dispatch["platform_family"] == PLATFORM_FAMILY
    assert dispatch["architecture"] == "x86_64"
    assert dispatch["device_class"] == DEVICE_CLASS
    assert dispatch["physical_execution"] == "not_executed"
    assert (
        detect_windows_dispatch(system="Windows", machine="x86_64")["device_class"]
        == DEVICE_CLASS
    )


@pytest.mark.parametrize(
    "system",
    [
        "Darwin",
        "Linux",
        "FreeBSD",
        # None observes this host, which is only a refusal off Windows.
        pytest.param(
            None,
            marks=pytest.mark.skipif(
                platform.system() == "Windows", reason="None observes a Windows host"
            ),
        ),
        7,
    ],
)
def test_non_windows_systems_are_refused_never_inferred(system) -> None:
    code = _code(lambda: detect_windows_dispatch(system=system, machine="AMD64"))
    assert code in {"unsupported_platform", "platform_observation_invalid"}


@pytest.mark.parametrize("machine", ["ARM64", "arm64", "i686", "aarch64", "unknown", 7])
def test_undeclared_windows_architectures_are_refused(machine) -> None:
    code = _code(lambda: detect_windows_dispatch(system="Windows", machine=machine))
    assert code in {"unsupported_architecture", "platform_observation_invalid"}


def test_provider_family_and_build_digest_must_be_declared_exactly() -> None:
    assert (
        _code(lambda: _declaration(provider_family="contoso_accelerator"))
        == "provider_family_unregistered"
    )
    assert (
        _code(lambda: _declaration(build_digest="sha256:short"))
        == "provider_build_digest_invalid"
    )
    assert (
        _code(lambda: _declaration(driver_runtime_version_digest="0" * 40))
        == "provider_build_digest_invalid"
    )


def test_declaration_is_closed_over_unknown_and_missing_fields() -> None:
    values = {
        "provider_family": "onednn",
        "build_digest": PROVIDER_DIGEST,
        "driver_runtime_identity": "windows_onednn",
        "driver_runtime_version_digest": _digest("b"),
        "activation_precisions": ["float16"],
        "persistent_state_boundary": "warmup",
        "decode_time_full_weight_materializations": 0,
        "per_decode_full_weight_cast": False,
        "stage_local_kv": True,
        "cancellation_supported": True,
        "cleanup_supported": True,
        "workspace_bounded_bytes": 1 << 20,
        "issued_at": NOW,
        "expires_at": NOW + 3600.0,
    }
    assert (
        _code(lambda: validate_windows_provider_declaration({**values, "extra": 1}))
        == "windows_provider_declaration_invalid"
    )
    missing = {key: item for key, item in values.items() if key != "stage_local_kv"}
    assert (
        _code(lambda: validate_windows_provider_declaration(missing))
        == "windows_provider_declaration_invalid"
    )


def test_activation_precision_must_be_a_supported_codec_dtype() -> None:
    assert "float16" in SUPPORTED_DTYPES
    assert (
        _code(lambda: _declaration(activation_precisions=["float32"]))
        == "provider_activation_precision_unsupported"
    )
    assert (
        _code(lambda: _declaration(activation_precisions=[]))
        == "provider_activation_precision_unsupported"
    )


def test_adapter_refuses_a_per_decode_whole_weight_cast_policy() -> None:
    assert (
        _code(lambda: _declaration(per_decode_full_weight_cast=True))
        == "provider_per_decode_cast_rejected"
    )
    assert (
        _code(lambda: _declaration(decode_time_full_weight_materializations=1))
        == "provider_materialization_policy_rejected"
    )
    assert (
        _code(lambda: _declaration(stage_local_kv=False)) == "provider_stage_local_kv_required"
    )


def test_provider_library_binding_is_digest_pinned_and_host_checked(tmp_path) -> None:
    bound = _binding(tmp_path)
    assert bound["bound"] is True
    assert bound["protocol"] == PROTOCOL
    assert bound["win32_binding"] is True
    assert bound["library_handle_obtained"] is True
    assert bound["physical_execution"] == "not_executed"
    assert bound["library_digest"] == PROVIDER_DIGEST

    assert (
        _code(lambda: _binding(tmp_path, raw=b"other", digest=_digest("9")))
        == "provider_library_digest_mismatch"
    )

    link = tmp_path / "link.dll"
    link.symlink_to(_write_library(tmp_path))
    assert (
        _code(
            lambda: bind_provider_library(
                library_path=link,
                expected_digest=PROVIDER_DIGEST,
                library_loader=lambda location: location,
                host_platform="win32",
            )
        )
        == "provider_library_absent"
    )


def test_binding_on_this_host_fails_closed_without_a_win32_loader(tmp_path) -> None:
    # No injected loader and no win32 host: the real win32 binding seam is absent,
    # so the adapter refuses instead of pretending the provider is available.
    assert (
        _code(
            lambda: bind_provider_library(
                library_path=_write_library(tmp_path),
                expected_digest=PROVIDER_DIGEST,
                host_platform="darwin",
            )
        )
        == "provider_library_binding_unavailable_on_host"
    )


def test_adapter_requires_a_bound_library_with_the_declared_build_digest(tmp_path) -> None:
    adapter, _, _, _ = _fixture(tmp_path)
    assert adapter.provider_report()["bound_library_digest"] == PROVIDER_DIGEST

    # A library bound to a different build than the declared provider build is refused.
    wrong = _binding(tmp_path, raw=b"different-build")
    with pytest.raises(WindowsProviderUnavailable) as caught:
        WindowsNativeProviderAdapter(
            declaration=_declaration(),
            dispatch=detect_windows_dispatch(system="Windows", machine="AMD64"),
            library=wrong,
            weight=np.ones((2, 2), dtype=np.int8),
            scales=np.ones(2, dtype=np.float32),
        )
    assert caught.value.code == "provider_library_digest_mismatch"

    with pytest.raises(WindowsProviderUnavailable) as caught:
        WindowsNativeProviderAdapter(
            declaration=_declaration(),
            dispatch=detect_windows_dispatch(system="Windows", machine="AMD64"),
            library={"bound": False},
            weight=np.ones((2, 2), dtype=np.int8),
            scales=np.ones(2, dtype=np.float32),
        )
    assert caught.value.code == "provider_not_bound"


def test_adapter_executes_the_landed_int8_kernel_seam_with_real_ledgers(tmp_path) -> None:
    adapter, weight, scales, activations = _fixture(tmp_path)
    adapter.warmup(activations)
    observed = adapter.linear(activations)
    expected = quantized_reference_linear(activations, weight, scales)
    assert np.array_equal(observed, expected)
    ledger = adapter.ledger()
    assert ledger.warmup_complete is True
    assert ledger.full_weight_materializations == 0
    assert ledger.bounded_slab_conversions > 0
    assert adapter.packed_state_bytes == int(weight.nbytes + scales.nbytes)
    assert adapter.packed_state_is_stable() is True
    adapter.assert_warm_decode()
    assert ledger.peak_weight_workspace_bytes <= 4096
    assert "native_x86_64" in adapter.dispatch_report()["dispatch"]


def test_adapter_never_claims_physical_acceleration(tmp_path) -> None:
    adapter, _, _, activations = _fixture(tmp_path)
    adapter.warmup(activations)
    report = adapter.physical_qualification_report()
    assert report["physical_execution"] == "not_executed"
    assert report["qualified"] is False
    assert report["accelerated"] is False
    assert report["blocker"] == "windows_x86_64_provider_not_physically_qualified"
    with pytest.raises(WindowsProviderUnavailable) as caught:
        adapter.assert_physical_qualification()
    assert caught.value.code == "windows_x86_64_provider_not_physically_qualified"
    # The source tranche carries no device observation anywhere in its reports.
    assert "native_observed" not in json.dumps(adapter.physical_qualification_report())


def test_adapter_emits_the_frozen_acceleration_profile_shape(tmp_path) -> None:
    adapter, weight, scales, _ = _fixture(tmp_path)
    profile_mapping = adapter.acceleration_profile()
    assert profile_mapping["protocol"] == ACCELERATION_PROTOCOL
    assert profile_mapping["backend_id"] == BACKEND_ID
    assert profile_mapping["device_class"] == DEVICE_CLASS
    profile = AccelerationProfile.from_mapping(profile_mapping)
    assert profile.decode_time_full_weight_materializations == 0
    assert profile.per_decode_full_weight_cast is False
    assert profile.packed_state_bounded_bytes == int(weight.nbytes + scales.nbytes)
    assert profile.activation_precisions == ("float16",)


def test_windows_acceleration_cases_stay_unearned_in_the_frozen_inventory() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert inventory["state"] == "design_only"
    cases = {case["case_id"]: case for case in inventory["acceptance_cases"]}
    assert cases["windows_accelerated_physical_positive"]["gate_kind"] == "physical_positive"
    for case_id in ACCEPTANCE_COVERAGE:
        assert case_id in cases
        assert ACCEPTANCE_COVERAGE[case_id].startswith("not_earned:")
        assert "physical" in ACCEPTANCE_COVERAGE[case_id]
