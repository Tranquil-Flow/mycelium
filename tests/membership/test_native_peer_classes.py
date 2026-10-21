# SPDX-License-Identifier: AGPL-3.0-only
"""Native Android and Windows peer classes (owner rulings 2026-09-27).

The native Android app is an activation-eligible peer class; Windows is a
joinable peer that stays activation-ineligible, with a named refusal, until a
Windows member runtime exists.  Both declarations are exact: a mismatched
runtime capability is refused rather than admitted under the class label.
"""

from __future__ import annotations

import pytest

from mycelium_membership import (
    JOIN_REQUEST_PROTOCOL,
    PEER_CLASS_ACTIVATION_REFUSALS,
    PEER_CLASSES,
    ROUTER_ACTIVATION_PROTOCOL,
    MembershipContractError,
    peer_runtime_activation_refusal,
    peer_runtime_is_activation_eligible,
    validate_membership_message,
    validate_peer_runtime_capability,
)
from mycelium_membership.contracts import _ACTIVATION_ELIGIBLE_PEER_CLASSES

ANDROID_NATIVE = {
    "runtime_backend": "mobile-rust-reference",
    "transport": "iroh",
    "activation_protocol": ROUTER_ACTIVATION_PROTOCOL,
}
WINDOWS_NATIVE = {
    "runtime_backend": "numpy",
    "transport": "iroh",
    "activation_protocol": ROUTER_ACTIVATION_PROTOCOL,
}


def _join_request(peer_class: str, runtime_capability: dict) -> dict:
    return {
        "protocol": JOIN_REQUEST_PROTOCOL,
        "message_id": "message-1",
        "swarm_id": "swarm-a",
        "sender_node_id": "node-a",
        "sender_endpoint_id": "endpoint-a",
        "recipient_node_id": "seed-node",
        "incarnation": "incarnation-a",
        "generation": 0,
        "issued_at": 1_000.0,
        "expires_at": 1_060.0,
        "invite_nonce": "invite-a",
        "endpoint_addr": {
            "id": "endpoint-a",
            "addrs": ["iroh-relay://relay.example/node-a"],
        },
        "software_version": "mycelium-test",
        "peer_class": peer_class,
        "runtime_capability": dict(runtime_capability),
    }


def test_native_classes_are_registered_members() -> None:
    assert {"android_native_iroh", "windows_native_iroh"} <= PEER_CLASSES
    for peer_class, capability in (
        ("android_native_iroh", ANDROID_NATIVE),
        ("windows_native_iroh", WINDOWS_NATIVE),
    ):
        assert validate_peer_runtime_capability(peer_class, capability) == capability


def test_android_native_iroh_is_activation_eligible_without_a_refusal() -> None:
    assert peer_runtime_is_activation_eligible("android_native_iroh", ANDROID_NATIVE)
    assert peer_runtime_activation_refusal("android_native_iroh") is None


def test_windows_native_iroh_is_activation_eligible() -> None:
    # Owner parity ruling 2026-10-07: the native Windows node is activation-eligible.
    assert peer_runtime_is_activation_eligible("windows_native_iroh", WINDOWS_NATIVE)
    assert peer_runtime_activation_refusal("windows_native_iroh") is None
    # With Linux parity applied too (2026-10-07), no known class carries a named refusal.
    assert PEER_CLASS_ACTIVATION_REFUSALS == {}


def test_activation_allow_set_is_closed_and_exact() -> None:
    assert _ACTIVATION_ELIGIBLE_PEER_CLASSES == frozenset(
        {"mac_mlx_iroh", "linux_numpy_iroh", "android_native_iroh", "windows_native_iroh",
         "linux_native_iroh", "android_native_qwen2_iroh"}
    )
    # Negative controls: every other class stays ineligible even when it
    # declares its own exact capability, and a refusal never names an
    # eligible class.
    assert not set(PEER_CLASS_ACTIVATION_REFUSALS) & _ACTIVATION_ELIGIBLE_PEER_CLASSES
    for peer_class in ("android_termux_iroh", "pixel_http", "browser_http", "linux_tbd"):
        assert peer_runtime_activation_refusal(peer_class) is None


@pytest.mark.parametrize("peer_class", [None, 7, ["windows_native_iroh"], "windows"])
def test_refusal_lookup_is_total_and_never_guesses(peer_class: object) -> None:
    assert peer_runtime_activation_refusal(peer_class) is None


@pytest.mark.parametrize(
    ("peer_class", "capability"),
    [
        # The Windows class is bound to its exact runtime and protocol.
        ("windows_native_iroh", {**WINDOWS_NATIVE, "activation_protocol": None}),
        ("windows_native_iroh", {**WINDOWS_NATIVE, "runtime_backend": "mlx"}),
        # The Android class is bound to its exact runtime, not to Termux's.
        ("android_native_iroh", {**ANDROID_NATIVE, "runtime_backend": "pixel-stdlib"}),
        ("android_native_iroh", {**ANDROID_NATIVE, "transport": "http"}),
        ("android_native_iroh", {**ANDROID_NATIVE, "activation_protocol": None}),
    ],
)
def test_native_class_capability_mismatch_is_refused(
    peer_class: str, capability: dict
) -> None:
    with pytest.raises(MembershipContractError) as refused:
        validate_membership_message(_join_request(peer_class, capability))
    assert refused.value.code == "membership_runtime_capability_mismatch"
    with pytest.raises(MembershipContractError):
        peer_runtime_is_activation_eligible(peer_class, capability)


def test_native_join_requests_validate() -> None:
    validate_membership_message(_join_request("android_native_iroh", ANDROID_NATIVE))
    validate_membership_message(_join_request("windows_native_iroh", WINDOWS_NATIVE))
    with pytest.raises(MembershipContractError) as unknown:
        validate_membership_message(_join_request("windows_onnx_iroh", WINDOWS_NATIVE))
    assert unknown.value.code == "membership_peer_class_invalid"
