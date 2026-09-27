# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate the Python-authored interop fixtures for mycelium-mobile-core.

Run from the repository root:
    python3 -B native/mobile_core/tests/fixtures/generate_fixtures.py

Every fixture is produced by the repository's own Python implementations
(signing, invite bundles, A12 Android onboarding), so the Rust tests prove byte
compatibility with the code the seed and owner console actually run.  Keys are
fixed test-only seeds; nothing here is a real credential.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402

from mycelium_invite.bundle import INVITE_BUNDLE_PROTOCOL, mint_invite_bundle  # noqa: E402
from mycelium_membership.contracts import JOIN_REQUEST_PROTOCOL, sign_membership_message  # noqa: E402
from mycelium_mobile import android_onboarding as onboarding  # noqa: E402
from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402
from mycelium_qualification.signing import Ed25519EvidenceSigner  # noqa: E402

OUT = Path(__file__).resolve().parent
NOW = 1_790_000_000.0


def signer(seed_byte: int, endpoint_id: str) -> Ed25519EvidenceSigner:
    private = Ed25519PrivateKey.from_private_bytes(bytes([seed_byte]) * 32)
    return Ed25519EvidenceSigner(
        endpoint_id=endpoint_id,
        _private_key=private,
        _public_key_bytes=private.public_key().public_bytes_raw(),
    )


def write(name: str, value: object) -> None:
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main() -> None:
    device = signer(0x11, "device-endpoint-1")
    seed = signer(0x22, "seed-endpoint-1")

    statement = {
        "protocol": "mycelium.fixture_statement.v1",
        "float_whole": 1790000000.0,
        "float_frac": 1790000000.125,
        "float_small": 0.0001,
        "float_tiny": 1.5e-05,
        "float_big": 1e16,
        "int": 42,
        "text": "café ☃ \"quoted\" \\ \n\t",
        "nested": {"b": [1, 2.5, None, True, False], "a": {}},
    }
    write(
        "signature.json",
        {
            "seed_hex": "11" * 32,
            "endpoint_id": device.endpoint_id,
            "statement": statement,
            "canonical_utf8": canonical_json_bytes(statement).decode("utf-8"),
            "public_key_record": device.public_key_record(),
            "signature": device.sign(statement),
        },
    )

    recipient = X25519PrivateKey.from_private_bytes(bytes([0x33]) * 32)
    nonce = "pair-fixture-nonce-000000000000000000000001"
    request_statement = {
        "protocol": onboarding.PAIRING_REQUEST_PROTOCOL,
        "device_ref": "device-ref-1",
        "identity_endpoint_id": device.endpoint_id,
        "identity_key_digest": device.verification_key_digest,
        "identity_verification_key": device.public_key_record(),
        "peer_class": onboarding.ANDROID_PEER_CLASS,
        "carrier": onboarding.NATIVE_CARRIER,
        "invite_protocol": INVITE_BUNDLE_PROTOCOL,
        "recipient_encryption_key": onboarding._b64encode(
            recipient.public_key().public_bytes_raw()
        ),
        "pairing_nonce": nonce,
        "issued_at": NOW,
        "expires_at": NOW + 300,
    }
    request = {**request_statement, "signature": device.sign(request_statement)}
    onboarding.verify_android_pairing_request(request, now=NOW + 1)

    bundle = mint_invite_bundle(
        signer=seed,
        swarm_id="swarm-fixture",
        seed_url="http://127.0.0.1:18765",
        ttl_seconds=240,
        nonce=nonce,
        issued_at=NOW,
    )
    quota = {
        "storage_bytes": 1 << 30,
        "network_bytes_per_day": 1 << 28,
        "background_allowed": True,
        "battery_floor_percent": 30,
        "thermal_ceiling_c": 42,
    }
    handoff = onboarding.approve_android_pairing_request(
        request=request, invite_bundle=bundle, quota=quota, now=NOW + 2
    )
    payload = onboarding.decrypt_android_invitation_handoff(
        request=request,
        handoff=handoff,
        recipient_private_key=recipient.private_bytes_raw(),
        now=NOW + 3,
    )
    write(
        "onboarding.json",
        {
            "now": NOW + 3,
            "recipient_private_hex": "33" * 32,
            "request": request,
            "request_digest": onboarding._digest(request),
            "handoff": handoff,
            "payload": payload,
        },
    )

    join = {
        "protocol": JOIN_REQUEST_PROTOCOL,
        "message_id": "msg-fixture-1",
        "swarm_id": "swarm-fixture",
        "sender_node_id": "node-phone-1",
        "sender_endpoint_id": device.endpoint_id,
        "recipient_node_id": "seed-node-1",
        "incarnation": "inc-fixture-1",
        "generation": 0,
        "issued_at": NOW,
        "expires_at": NOW + 60.0,
        "invite_nonce": nonce,
        "software_version": "mycelium-node-app/0.1.0",
        "peer_class": "android_termux_iroh",
        "runtime_capability": {
            "runtime_backend": "pixel-stdlib",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        "endpoint_addr": {"id": device.endpoint_id, "addrs": ["127.0.0.1:1"]},
    }
    write("join_envelope.json", sign_membership_message(signer=device, message=join))


if __name__ == "__main__":
    main()
