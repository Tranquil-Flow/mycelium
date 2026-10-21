# SPDX-License-Identifier: AGPL-3.0-only
"""A12 Android ADB/bridge connectivity path (SOURCE tranche, operator-only).

ADB is an EXCLUDED development path (specification sections 3 and 6, inventory
``excluded_normal_product_paths``).  These tests pin the whole point of the
module: the bridge can attach a device, speak one bounded capability handshake
and activate a development session, and it can never author a native
observation, a rung, a grant or a product-path activation.
"""

from __future__ import annotations

import json

import pytest

import mycelium_mobile.a12_activation as a12
from mycelium_mobile.android_adb_bridge import (
    ADB_BRIDGE_PROTOCOL,
    ATTACHMENT_PROTOCOL,
    BRIDGE_SESSION_PROTOCOL,
    CAPABILITY_HANDSHAKE_PROTOCOL,
    BRIDGE_SOURCE_KINDS,
    DEVICE_ATTACHMENT_STATES,
    PRODUCT_PATH_KIND,
    TOOL_LABEL,
    AdbBridgeError,
    activate_android_bridge_session,
    android_capability_handshake,
    attach_android_device,
    authorize_product_activation_from_bridge,
    parse_adb_devices,
)

NOW = 2_000_000_000.0
ALIAS = "alias:" + "ab" * 16
LISTING = """List of devices attached
R5CT30ANDROID01\tdevice usb:337641472X product:husky model:Pixel_8 device:husky transport_id:1
emulator-5554\tunauthorized transport_id:2
"""


def _code(call):
    with pytest.raises((AdbBridgeError, a12.A12ContractError)) as caught:
        call()
    return caught.value.code


def _attachment(**overrides):
    arguments = {
        "device_alias": ALIAS,
        "transport": "usb",
        "state": "device",
        "adb_version": "1.0.41",
        "now": NOW,
    }
    arguments.update(overrides)
    return attach_android_device(**arguments)


def _handshake(attachment, source_kind: str = "termux_conformance", **overrides):
    arguments = {
        "attachment": attachment,
        "source_kind": source_kind,
        "request_id": "handshake-0001",
        "requested_capabilities": ["memory", "thermal", "lifecycle"],
        "observed": {"memory": "unknown", "thermal": "unknown", "lifecycle": "unknown"},
        "now": NOW + 1,
    }
    arguments.update(overrides)
    return android_capability_handshake(**arguments)


def test_the_bridge_is_labelled_a_development_tool_and_never_a_product_path() -> None:
    assert TOOL_LABEL == "operator_development_conformance_tool"
    assert PRODUCT_PATH_KIND == "adb"
    assert PRODUCT_PATH_KIND in a12.EXCLUDED_NORMAL_PRODUCT_PATHS
    assert (
        _code(lambda: a12.require_undivided_product_path(PRODUCT_PATH_KIND))
        == "excluded_development_path_cannot_qualify"
    )
    assert ADB_BRIDGE_PROTOCOL == "mycelium.a12_android_adb_bridge.v1"
    assert ATTACHMENT_PROTOCOL == "mycelium.a12_android_device_attachment.v1"
    assert CAPABILITY_HANDSHAKE_PROTOCOL == "mycelium.a12_android_capability_handshake.v1"
    assert BRIDGE_SESSION_PROTOCOL == "mycelium.a12_android_bridge_session.v1"
    assert DEVICE_ATTACHMENT_STATES == ("device", "offline", "unauthorized")
    assert set(BRIDGE_SOURCE_KINDS) <= set(a12.NON_NATIVE_SOURCE_KINDS)
    assert a12.NATIVE_SOURCE_KIND not in BRIDGE_SOURCE_KINDS


def test_adb_devices_parsing_never_lets_a_raw_hardware_serial_out() -> None:
    attached = parse_adb_devices(LISTING)
    assert [entry["state"] for entry in attached] == ["device", "unauthorized"]
    assert all("R5CT30ANDROID01" not in json.dumps(entry) for entry in attached)
    assert all("emulator-5554" not in json.dumps(entry) for entry in attached)
    assert all(entry["device_alias"].startswith("alias:") for entry in attached)
    assert attached[0]["device_alias"] != attached[1]["device_alias"]

    with pytest.raises(AdbBridgeError) as caught:
        parse_adb_devices("List of devices attached\n")
    assert caught.value.code == "adb_bridge_no_devices_attached"


def test_an_attached_device_record_is_closed_pseudonymous_and_grants_nothing() -> None:
    record = _attachment()
    assert record["protocol"] == ATTACHMENT_PROTOCOL
    assert record["tool_label"] == TOOL_LABEL
    assert record["product_path_kind"] == PRODUCT_PATH_KIND
    assert record["authorizes_product_path"] is False
    assert record["grants"] == ()
    assert record["state"] == "device"
    assert record["transport"] == "usb"
    assert record["device_alias"] == ALIAS
    assert record["attached_at"] == NOW


def test_a_raw_serial_a_branded_transport_and_an_unknown_state_are_refused() -> None:
    assert (
        _code(lambda: _attachment(device_alias="R5CT30ANDROID01"))
        == "adb_bridge_raw_device_serial_rejected"
    )
    assert _code(lambda: _attachment(transport="wifi-direct")) == "adb_bridge_transport_invalid"
    assert _code(lambda: _attachment(state="weird")) == "adb_bridge_state_invalid"
    assert _code(lambda: _attachment(adb_version="")) == "adb_bridge_version_invalid"


def test_an_offline_device_cannot_be_handshaken_with() -> None:
    for state in ("offline", "unauthorized"):
        record = _attachment(state=state)
        assert (
            _code(lambda record=record: _handshake(record))
            == "adb_bridge_device_not_attached"
        )


def test_the_capability_handshake_is_bounded_closed_and_attachment_bound() -> None:
    attachment = _attachment()
    handshake = _handshake(attachment)
    assert handshake["protocol"] == CAPABILITY_HANDSHAKE_PROTOCOL
    assert handshake["tool_label"] == TOOL_LABEL
    assert handshake["source_kind"] == "termux_conformance"
    assert handshake["request_id"] == "handshake-0001"
    assert handshake["observed_at"] == NOW + 1
    assert handshake["authorizes_product_path"] is False
    assert handshake["grants"] == ()
    assert handshake["rung_effect"] == "none"

    other = _attachment(device_alias="alias:" + "cd" * 16)
    assert (
        _code(lambda: _handshake(other, attachment_digest=handshake["attachment_digest"]))
        == "adb_bridge_handshake_binding_invalid"
    )
    assert (
        _code(lambda: _handshake(attachment, request_id=""))
        == "adb_bridge_handshake_invalid"
    )
    assert (
        _code(lambda: _handshake(attachment, requested_capabilities=("memory", "memory")))
        == "adb_bridge_handshake_invalid"
    )
    assert (
        _code(lambda: _handshake(attachment, observed={"memory": 1}))
        == "adb_bridge_handshake_invalid"
    )


def test_the_bridge_can_never_emit_a_native_observation() -> None:
    attachment = _attachment()
    assert (
        _code(lambda: _handshake(attachment, source_kind=a12.NATIVE_SOURCE_KIND))
        == "adb_bridge_cannot_emit_native_observation"
    )
    assert (
        _code(lambda: _handshake(attachment, source_kind="guesswork"))
        == "adb_bridge_source_kind_invalid"
    )
    handshake = _handshake(attachment)
    assert (
        _code(lambda: a12.require_native_observation(handshake["source_kind"]))
        == "non_native_source_cannot_qualify"
    )


def test_an_activated_bridge_session_still_starts_route_ready_false() -> None:
    attachment = _attachment()
    handshake = _handshake(attachment)
    session = activate_android_bridge_session(
        attachment=attachment, handshake=handshake, now=NOW + 2
    )
    assert session["protocol"] == BRIDGE_SESSION_PROTOCOL
    assert session["route_ready"] is False
    assert session["authority"] == TOOL_LABEL
    assert session["product_path_kind"] == PRODUCT_PATH_KIND
    assert session["grants"] == ()
    assert session["rung_effect"] == "none"
    assert set(session["permitted_scopes"]) == {"conformance_probe", "diagnostics"}

    stale = activate_android_bridge_session(
        attachment=attachment, handshake=handshake, now=NOW - 5
    )
    assert stale["session_id"] != session["session_id"]


def test_a_session_cannot_be_activated_from_a_foreign_or_labelled_handshake() -> None:
    attachment = _attachment()
    handshake = _handshake(attachment)
    forged = {**handshake, "tool_label": "signed_native_application"}
    assert (
        _code(
            lambda: activate_android_bridge_session(
                attachment=attachment, handshake=forged, now=NOW + 2
            )
        )
        == "adb_bridge_handshake_invalid"
    )


def test_product_activation_from_the_bridge_always_fails_closed() -> None:
    attachment = _attachment()
    handshake = _handshake(attachment)
    assert (
        _code(
            lambda: authorize_product_activation_from_bridge(
                attachment=attachment, handshake=handshake, path_kind="signed_native_application"
            )
        )
        == "excluded_development_path_cannot_qualify"
    )
    assert (
        _code(
            lambda: authorize_product_activation_from_bridge(
                attachment=attachment, handshake=handshake, path_kind=PRODUCT_PATH_KIND
            )
        )
        == "excluded_development_path_cannot_qualify"
    )


def test_the_refusal_a_caller_observes_is_the_policy_code_not_a_bridge_owned_one() -> None:
    """Honest error surface: the documented refusal is the one that is raised.

    The audit found ``authorize_product_activation_from_bridge`` carrying a
    refusal branch that cannot be reached as written.  This test pins which code
    a caller actually gets, so the docstring cannot drift back into advertising
    an unobservable one.
    """

    import mycelium_mobile.android_adb_bridge as bridge_module

    attachment = _attachment()
    handshake = _handshake(attachment)
    documented = bridge_module.authorize_product_activation_from_bridge

    assert documented.__doc__ is not None
    assert "excluded_development_path_cannot_qualify" in documented.__doc__
    assert "ignored" in documented.__doc__

    with pytest.raises(a12.A12ContractError) as caught:
        authorize_product_activation_from_bridge(
            attachment=attachment,
            handshake=handshake,
            path_kind="signed_native_application",
        )
    assert caught.value.code == "excluded_development_path_cannot_qualify"
    assert str(caught.value) == "excluded_development_path_cannot_qualify"


def test_bridge_owned_refusals_are_reached_when_the_label_or_the_policy_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge's own fail-closed refusals are reachable, so they are guards.

    ``adb_bridge_product_path_label_invalid`` fires when the module's registered
    label is no longer an excluded path, and
    ``adb_bridge_product_activation_unreachable`` fires when the excluded-path
    policy stops refusing a label this function has already proven excluded --
    the case that used to be dead code.  Both are simulated here with the
    production prefix of the function (attachment + handshake) left intact.
    """

    import mycelium_mobile.android_adb_bridge as bridge_module

    attachment = _attachment()
    handshake = _handshake(attachment)

    with monkeypatch.context() as patch:
        patch.setattr(bridge_module, "PRODUCT_PATH_KIND", "signed_native_application")
        # The attachment record carries the module's label, so a drifted label
        # passes the attachment check and reaches the bridge's own guard.
        drifted_attachment = _attachment()
        drifted_handshake = _handshake(drifted_attachment)
        with pytest.raises(AdbBridgeError) as drifted:
            authorize_product_activation_from_bridge(
                attachment=drifted_attachment, handshake=drifted_handshake
            )
        assert drifted.value.code == "adb_bridge_product_path_label_invalid"

    with monkeypatch.context() as patch:
        patch.setattr(
            bridge_module,
            "require_undivided_product_path",
            lambda path_kind: str(path_kind),
        )
        with pytest.raises(AdbBridgeError) as terminal:
            bridge_module.authorize_product_activation_from_bridge(
                attachment=attachment, handshake=handshake
            )
        assert terminal.value.code == "adb_bridge_product_activation_unreachable"

    # Unpatched: the excluded-path policy refuses, so neither bridge-owned code
    # is observable and no activation is returned.
    assert (
        _code(
            lambda: authorize_product_activation_from_bridge(
                attachment=attachment, handshake=handshake
            )
        )
        == "excluded_development_path_cannot_qualify"
    )
