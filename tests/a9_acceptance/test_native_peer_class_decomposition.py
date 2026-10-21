# SPDX-License-Identifier: AGPL-3.0-only
"""v2 decomposition of the native Android and Windows legacy peer classes.

Both are enumerated so every shipped v1 class stays mapped.  Both stay
``declared_ineligible`` in the v2 declaration until class-qualification evidence
exists, the same stance ``linux_numpy_iroh`` already takes although the v1
activation allow-set admits it; the class label never mints a runtime, an
architecture or v2 eligibility.
"""

from __future__ import annotations

from mycelium_membership import PEER_CLASSES, peer_runtime_is_activation_eligible
from mycelium_platform_neutral import migration


def test_native_classes_decompose_without_guessing() -> None:
    android = migration.decompose_legacy_peer_class("android_native_iroh")
    assert android.to_mapping() == {
        "peer_class": "android_native_iroh",
        "identity_kind": "native_peer",
        "platform_family": "android",
        "architecture": "unknown",
        "transports": ["iroh"],
        "runtimes": [],
        "declared_ineligible": True,
    }
    windows = migration.decompose_legacy_peer_class("windows_native_iroh")
    assert windows.to_mapping() == {
        "peer_class": "windows_native_iroh",
        "identity_kind": "native_peer",
        "platform_family": "windows",
        "architecture": "unknown",
        "transports": ["iroh"],
        "runtimes": ["numpy"],
        "declared_ineligible": True,
    }


def test_linux_native_class_decomposes_to_its_bound_numpy_runtime() -> None:
    """The A13 Linux product class binds the NumPy runtime (owner parity ruling
    2026-10-07); v2 eligibility still needs class qualification."""

    linux = migration.decompose_legacy_peer_class("linux_native_iroh")
    assert linux.to_mapping() == {
        "peer_class": "linux_native_iroh",
        "identity_kind": "native_peer",
        "platform_family": "linux",
        "architecture": "unknown",
        "transports": ["iroh"],
        "runtimes": ["numpy"],
        "declared_ineligible": True,
    }
    assert migration.decompose_legacy_peer_class("linux_numpy_iroh").runtimes == (
        "numpy",
    )


def test_decomposition_covers_every_shipped_class_exactly() -> None:
    assert set(migration.LEGACY_PEER_CLASS_DECOMPOSITION) == set(PEER_CLASSES)


def test_v1_eligibility_does_not_imply_v2_declared_eligibility() -> None:
    """Only the qualified mac class is declared eligible in v2 (negative control)."""

    declared_eligible = {
        name
        for name, record in migration.LEGACY_PEER_CLASS_DECOMPOSITION.items()
        if not record["declared_ineligible"]
    }
    assert declared_eligible == {"mac_mlx_iroh"}
    assert peer_runtime_is_activation_eligible(
        "android_native_iroh",
        {
            "runtime_backend": "mobile-rust-reference",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
    )
