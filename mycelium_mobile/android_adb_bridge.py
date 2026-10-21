# SPDX-License-Identifier: AGPL-3.0-only
"""Android ADB/bridge connectivity path for A12 (operator-only, SOURCE tranche).

ADB is an EXCLUDED development path.  The inventory's
``excluded_normal_product_paths`` is ``["termux", "adb", "ssh"]`` and the
specification says such operator tools "may validate protocol portability during
development, but cannot satisfy native installation, native lifecycle,
background, thermal/power, or general Android gates".  This module therefore
implements the connectivity path — device attachment, one bounded capability
handshake, development-session activation — and the refusals that make the
boundary real rather than declarative:

* every record it produces is labelled
  ``operator_development_conformance_tool`` and carries
  ``product_path_kind="adb"``, ``authorizes_product_path=False``,
  ``grants=()`` and ``rung_effect="none"``;
* the handshake may only carry a non-native source kind; a bridge that claims
  ``native_device_observation`` is refused with
  ``adb_bridge_cannot_emit_native_observation``, so no eligibility proof can be
  built on bridge output (``a12_activation.require_native_observation`` rejects
  the labels this module is allowed to emit);
* an activated session still starts ``route_ready=false``;
* ``authorize_product_activation_from_bridge`` always fails closed with
  ``excluded_development_path_cannot_qualify``, whatever path kind the caller
  claims.

Device attachment never carries a raw hardware serial: ``parse_adb_devices``
reduces the ADB listing to pseudonymous aliases, and ``attach_android_device``
refuses any alias that is not already pseudonymous.

NOT claimed here: an attached device, a performed handshake, a live session, a
native observation, a qualification, or an installation path.  Nothing in this
module runs ``adb``, spawns a process, opens a socket, or enables a device.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
import math
import re
from typing import Any, NoReturn

from mycelium_qualification.evidence import canonical_json_bytes

from .a12_activation import (
    EXCLUDED_NORMAL_PRODUCT_PATHS,
    NATIVE_SOURCE_KIND,
    NON_NATIVE_SOURCE_KINDS,
    require_route_ready_is_false,
    require_undivided_product_path,
)

ADB_BRIDGE_PROTOCOL = "mycelium.a12_android_adb_bridge.v1"
ATTACHMENT_PROTOCOL = "mycelium.a12_android_device_attachment.v1"
CAPABILITY_HANDSHAKE_PROTOCOL = "mycelium.a12_android_capability_handshake.v1"
BRIDGE_SESSION_PROTOCOL = "mycelium.a12_android_bridge_session.v1"

#: The label every record of this path carries.  It is never a product path.
TOOL_LABEL = "operator_development_conformance_tool"
PRODUCT_PATH_KIND = "adb"
DEVICE_ATTACHMENT_STATES: tuple[str, ...] = ("device", "offline", "unauthorized")
BRIDGE_TRANSPORTS: tuple[str, ...] = ("usb", "tcp")

#: Source kinds the bridge is allowed to label its output with.  A bridge may
#: never produce a native device observation.
BRIDGE_SOURCE_KINDS: tuple[str, ...] = ("termux_conformance", "operator_declared")

PERMITTED_SCOPES: tuple[str, ...] = ("conformance_probe", "diagnostics")
MAX_REQUESTED_CAPABILITIES = 8

_ALIAS_RE = re.compile(r"^(?:sha256:[0-9a-f]{64}|alias:[0-9a-f]{8,64})$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OBSERVATION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.\-]{0,31}$")
_ALIAS_DIGEST_LENGTH = 32

_ATTACHMENT_FIELDS = frozenset(
    {
        "protocol",
        "tool_label",
        "product_path_kind",
        "authorizes_product_path",
        "grants",
        "device_alias",
        "transport",
        "state",
        "adb_version",
        "attached_at",
    }
)
_HANDSHAKE_FIELDS = frozenset(
    {
        "protocol",
        "tool_label",
        "product_path_kind",
        "authorizes_product_path",
        "grants",
        "rung_effect",
        "attachment_digest",
        "request_id",
        "source_kind",
        "requested_capabilities",
        "observed",
        "observed_at",
    }
)
_SESSION_FIELDS = frozenset(
    {
        "protocol",
        "session_id",
        "tool_label",
        "product_path_kind",
        "authority",
        "route_ready",
        "permitted_scopes",
        "grants",
        "rung_effect",
        "attachment_digest",
        "request_id",
        "started_at",
    }
)


class AdbBridgeError(RuntimeError):
    """Fail-closed bridge error carrying a stable code and no device identity."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _finite(value: Any, *, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise AdbBridgeError(code)
    return float(value)


def _digest(document: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(document))).hexdigest()


def _alias_for(raw_serial: str) -> str:
    """Reduce one raw ADB serial to a stable pseudonymous alias."""

    return "alias:" + hashlib.sha256(raw_serial.encode("utf-8")).hexdigest()[
        :_ALIAS_DIGEST_LENGTH
    ]


def parse_adb_devices(listing: object) -> tuple[dict[str, Any], ...]:
    """Parse an ``adb devices`` listing into pseudonymous attachment entries.

    A raw hardware serial never leaves this function: the alias is a SHA-256
    prefix of the serial, which keeps a device comparable across reconnects
    without publishing an identifier (the privacy projection of specification
    section 5 excludes serial numbers outright).
    """

    if not isinstance(listing, str):
        raise AdbBridgeError("adb_bridge_listing_invalid")
    entries: list[dict[str, Any]] = []
    for line in listing.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("List of devices attached"):
            continue
        fields = stripped.split()
        if len(fields) < 2:
            raise AdbBridgeError("adb_bridge_listing_invalid")
        raw_serial, state = fields[0], fields[1]
        if state not in DEVICE_ATTACHMENT_STATES:
            raise AdbBridgeError("adb_bridge_state_invalid")
        entries.append(
            {
                "device_alias": _alias_for(raw_serial),
                "state": state,
                "transport": "tcp" if raw_serial.startswith("emulator-") else "usb",
            }
        )
    if not entries:
        raise AdbBridgeError("adb_bridge_no_devices_attached")
    return tuple(entries)


def attach_android_device(
    *,
    device_alias: object,
    transport: object,
    state: object,
    adb_version: object,
    now: object,
) -> dict[str, Any]:
    """Record one attached device as an operator development attachment."""

    if not isinstance(device_alias, str) or _ALIAS_RE.fullmatch(device_alias) is None:
        raise AdbBridgeError("adb_bridge_raw_device_serial_rejected")
    if transport not in BRIDGE_TRANSPORTS:
        raise AdbBridgeError("adb_bridge_transport_invalid")
    if state not in DEVICE_ATTACHMENT_STATES:
        raise AdbBridgeError("adb_bridge_state_invalid")
    if not isinstance(adb_version, str) or _VERSION_RE.fullmatch(adb_version) is None:
        raise AdbBridgeError("adb_bridge_version_invalid")
    return {
        "protocol": ATTACHMENT_PROTOCOL,
        "tool_label": TOOL_LABEL,
        "product_path_kind": PRODUCT_PATH_KIND,
        "authorizes_product_path": False,
        "grants": (),
        "device_alias": device_alias,
        "transport": str(transport),
        "state": str(state),
        "adb_version": adb_version,
        "attached_at": _finite(now, code="adb_bridge_time_invalid"),
    }


def _require_attachment(attachment: object) -> dict[str, Any]:
    if not isinstance(attachment, Mapping) or set(attachment) != _ATTACHMENT_FIELDS:
        raise AdbBridgeError("adb_bridge_attachment_invalid")
    value = dict(attachment)
    if (
        value.get("protocol") != ATTACHMENT_PROTOCOL
        or value.get("tool_label") != TOOL_LABEL
        or value.get("product_path_kind") != PRODUCT_PATH_KIND
        or value.get("authorizes_product_path") is not False
        or tuple(value.get("grants") or ()) != ()
    ):
        raise AdbBridgeError("adb_bridge_attachment_invalid")
    if value.get("state") != "device":
        raise AdbBridgeError("adb_bridge_device_not_attached")
    return value


def android_capability_handshake(
    *,
    attachment: object,
    source_kind: object,
    request_id: object,
    requested_capabilities: object,
    observed: object,
    now: object,
    attachment_digest: object = None,
) -> dict[str, Any]:
    """Run one bounded, closed capability handshake over the development bridge.

    The response must reference the attachment it came from, must name a
    capability family for every capability requested, and must label its source
    kind as non-native.  A missing observation is the string ``unknown`` — never
    zero, healthy or inferred support.
    """

    record = _require_attachment(attachment)
    if source_kind == NATIVE_SOURCE_KIND:
        raise AdbBridgeError("adb_bridge_cannot_emit_native_observation")
    if source_kind not in BRIDGE_SOURCE_KINDS or source_kind not in NON_NATIVE_SOURCE_KINDS:
        raise AdbBridgeError("adb_bridge_source_kind_invalid")
    if not isinstance(request_id, str) or _SEGMENT_RE.fullmatch(request_id) is None:
        raise AdbBridgeError("adb_bridge_handshake_invalid")
    if (
        isinstance(requested_capabilities, (str, bytes))
        or not isinstance(requested_capabilities, Sequence)
        or not 1 <= len(requested_capabilities) <= MAX_REQUESTED_CAPABILITIES
    ):
        raise AdbBridgeError("adb_bridge_handshake_invalid")
    requested = list(requested_capabilities)
    for name in requested:
        if not isinstance(name, str) or _SEGMENT_RE.fullmatch(name) is None:
            raise AdbBridgeError("adb_bridge_handshake_invalid")
    if len(set(requested)) != len(requested):
        raise AdbBridgeError("adb_bridge_handshake_invalid")
    if not isinstance(observed, Mapping) or set(observed) != set(requested):
        raise AdbBridgeError("adb_bridge_handshake_invalid")
    for name, value in observed.items():
        if not isinstance(value, str) or _OBSERVATION_RE.fullmatch(value) is None:
            raise AdbBridgeError("adb_bridge_handshake_invalid")

    digest = _digest(record)
    if attachment_digest is not None and attachment_digest != digest:
        raise AdbBridgeError("adb_bridge_handshake_binding_invalid")
    return {
        "protocol": CAPABILITY_HANDSHAKE_PROTOCOL,
        "tool_label": TOOL_LABEL,
        "product_path_kind": PRODUCT_PATH_KIND,
        "authorizes_product_path": False,
        "grants": (),
        "rung_effect": "none",
        "attachment_digest": digest,
        "request_id": request_id,
        "source_kind": str(source_kind),
        "requested_capabilities": tuple(requested),
        "observed": {name: str(observed[name]) for name in requested},
        "observed_at": _finite(now, code="adb_bridge_time_invalid"),
    }


def _require_handshake(handshake: object, *, attachment_digest: str) -> dict[str, Any]:
    if not isinstance(handshake, Mapping) or set(handshake) != _HANDSHAKE_FIELDS:
        raise AdbBridgeError("adb_bridge_handshake_invalid")
    value = dict(handshake)
    if (
        value.get("protocol") != CAPABILITY_HANDSHAKE_PROTOCOL
        or value.get("tool_label") != TOOL_LABEL
        or value.get("product_path_kind") != PRODUCT_PATH_KIND
        or value.get("authorizes_product_path") is not False
        or tuple(value.get("grants") or ()) != ()
        or value.get("rung_effect") != "none"
        or value.get("attachment_digest") != attachment_digest
        or value.get("source_kind") not in BRIDGE_SOURCE_KINDS
    ):
        raise AdbBridgeError("adb_bridge_handshake_invalid")
    return value


def activate_android_bridge_session(
    *, attachment: object, handshake: object, now: object
) -> dict[str, Any]:
    """Activate one operator development session; ``route_ready`` stays false."""

    record = _require_attachment(attachment)
    digest = _digest(record)
    verified = _require_handshake(handshake, attachment_digest=digest)
    started_at = _finite(now, code="adb_bridge_time_invalid")
    require_route_ready_is_false(False)
    session_id = "bridge-" + hashlib.sha256(
        canonical_json_bytes(
            {
                "attachment_digest": digest,
                "request_id": verified["request_id"],
                "started_at": started_at,
            }
        )
    ).hexdigest()[:32]
    return {
        "protocol": BRIDGE_SESSION_PROTOCOL,
        "session_id": session_id,
        "tool_label": TOOL_LABEL,
        "product_path_kind": PRODUCT_PATH_KIND,
        "authority": TOOL_LABEL,
        "route_ready": False,
        "permitted_scopes": PERMITTED_SCOPES,
        "grants": (),
        "rung_effect": "none",
        "attachment_digest": digest,
        "request_id": verified["request_id"],
        "started_at": started_at,
    }


def authorize_product_activation_from_bridge(
    *,
    attachment: object,
    handshake: object,
    path_kind: object = PRODUCT_PATH_KIND,
) -> NoReturn:
    """Always refuse: the bridge is not the ordinary product path.

    ``path_kind`` is accepted for call-site compatibility and then deliberately
    **ignored** -- it is never read.  The refusal is computed from the bridge's
    own registered label (``PRODUCT_PATH_KIND``), never from the caller's claim,
    so no argument can turn a development session into a product activation
    (inventory decision ``native_product_path_only``).

    The refusal a caller actually observes is the excluded-path policy code
    ``excluded_development_path_cannot_qualify``, raised by
    :func:`require_undivided_product_path`.  The two ``AdbBridgeError`` codes
    below are this module's own fail-closed guards and are reached only when the
    label or the policy has moved: ``adb_bridge_product_path_label_invalid`` when
    ``PRODUCT_PATH_KIND`` is no longer registered as an excluded path, and
    ``adb_bridge_product_activation_unreachable`` when the excluded-path policy
    stops refusing a label this function has already proven excluded.
    """

    record = _require_attachment(attachment)
    _require_handshake(handshake, attachment_digest=_digest(record))
    del path_kind  # never consulted: the caller's label is not an authority here
    if PRODUCT_PATH_KIND not in EXCLUDED_NORMAL_PRODUCT_PATHS:
        raise AdbBridgeError("adb_bridge_product_path_label_invalid")
    require_undivided_product_path(PRODUCT_PATH_KIND)
    # Reached only if the excluded-path policy stops refusing the bridge's own
    # excluded label; the bridge then refuses in its own voice instead of
    # returning an activation.
    raise AdbBridgeError("adb_bridge_product_activation_unreachable")


def bridge_status() -> dict[str, Any]:
    """The closed, self-describing boundary of this path.  Rights: none."""

    return {
        "protocol": ADB_BRIDGE_PROTOCOL,
        "tool_label": TOOL_LABEL,
        "product_path_kind": PRODUCT_PATH_KIND,
        "excluded_product_path": PRODUCT_PATH_KIND in EXCLUDED_NORMAL_PRODUCT_PATHS,
        "source_kinds": list(BRIDGE_SOURCE_KINDS),
        "native_source_kind_reachable": False,
        "route_ready_after_activation": False,
        "permitted_scopes": list(PERMITTED_SCOPES),
        "authorizes_product_path": False,
        "claim_boundary": (
            "operator development and conformance connectivity tooling only; no "
            "device attachment, handshake, session, native observation, "
            "qualification or installation path is performed or claimed"
        ),
    }
