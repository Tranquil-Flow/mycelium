# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from mycelium_a13_macos.pairing import (
    PairingError,
    approve_pairing_request,
    decrypt_invitation_handoff,
    new_pairing_request,
)
from mycelium_invite import mint_invite_bundle
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 2_000_000_000.0


def _flip(value: str) -> str:
    return ("B" if value[0] == "A" else "A") + value[1:]


QUOTA = {
    "storage_bytes": 2_000_000_000,
    "network_bytes_per_day": 5_000_000_000,
    "background_allowed": False,
}


def _case():
    target = generate_ed25519_signer(endpoint_id="target-endpoint")
    seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
    request, private_key = new_pairing_request(
        signer=target,
        node_id="judge-mac",
        peer_class="mac_mlx_iroh",
        now=NOW,
        ttl_seconds=300,
        nonce="pairing-nonce-1",
    )
    bundle = mint_invite_bundle(
        signer=seed,
        swarm_id="judge-swarm",
        seed_url="https://127.0.0.1:4443",
        ttl_seconds=120,
        nonce="pairing-nonce-1",
        issued_at=NOW,
    )
    handoff = approve_pairing_request(
        request=request,
        invite_bundle=bundle,
        quota=QUOTA,
        now=NOW,
    )
    return request, private_key, bundle, handoff


def test_recipient_encrypted_handoff_roundtrip_binds_all_approval_fields() -> None:
    request, private_key, bundle, handoff = _case()

    rendered_request = canonical_json_bytes(request)
    rendered_handoff = canonical_json_bytes(handoff)
    assert bundle["token"].encode() not in rendered_request
    assert bundle["token"].encode() not in rendered_handoff
    assert set(handoff) == {
        "protocol",
        "request_digest",
        "recipient_key_digest",
        "sender_ephemeral_key",
        "kdf_salt",
        "aead_nonce",
        "expires_at",
        "ciphertext",
    }

    recovered = decrypt_invitation_handoff(
        request=request,
        handoff=handoff,
        recipient_private_key=private_key,
        now=NOW,
    )
    assert recovered["invite_bundle"] == bundle
    assert recovered["node_id"] == "judge-mac"
    assert recovered["identity_key_digest"] == request["identity_key_digest"]
    assert recovered["pairing_nonce"] == "pairing-nonce-1"
    assert recovered["peer_class"] == "mac_mlx_iroh"
    assert recovered["seed_key_digest"] == bundle["seed_key_digest"]
    assert recovered["quota"] == QUOTA
    assert recovered["expires_at"] == NOW + 120


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.__setitem__("ciphertext", _flip(value["ciphertext"])),
        lambda value: value.__setitem__("expires_at", value["expires_at"] - 1),
        lambda value: value.__setitem__("request_digest", "sha256:" + "0" * 64),
    ],
)
def test_handoff_rejects_tampering(mutation) -> None:
    request, private_key, _bundle, handoff = _case()
    changed = copy.deepcopy(handoff)
    mutation(changed)
    with pytest.raises(
        PairingError, match="pairing_handoff_(invalid|request_mismatch)"
    ):
        decrypt_invitation_handoff(
            request=request,
            handoff=changed,
            recipient_private_key=private_key,
            now=NOW,
        )


def test_handoff_rejects_wrong_recipient_and_changed_request() -> None:
    request, _private_key, _bundle, handoff = _case()
    other_signer = generate_ed25519_signer(endpoint_id="other-endpoint")
    other_request, other_private_key = new_pairing_request(
        signer=other_signer,
        node_id="other-mac",
        peer_class="mac_mlx_iroh",
        now=NOW,
        ttl_seconds=300,
        nonce="pairing-nonce-2",
    )
    with pytest.raises(PairingError, match="pairing_handoff_recipient_mismatch"):
        decrypt_invitation_handoff(
            request=request,
            handoff=handoff,
            recipient_private_key=other_private_key,
            now=NOW,
        )
    with pytest.raises(PairingError, match="pairing_handoff_request_mismatch"):
        decrypt_invitation_handoff(
            request=other_request,
            handoff=handoff,
            recipient_private_key=other_private_key,
            now=NOW,
        )


def test_request_and_handoff_expiry_fail_closed() -> None:
    request, private_key, _bundle, handoff = _case()
    with pytest.raises(PairingError, match="pairing_handoff_expired"):
        decrypt_invitation_handoff(
            request=request,
            handoff=handoff,
            recipient_private_key=private_key,
            now=NOW + 121,
        )
    with pytest.raises(PairingError, match="pairing_request_expired"):
        approve_pairing_request(
            request=request,
            invite_bundle=_case()[2],
            quota=QUOTA,
            now=NOW + 301,
        )


def test_owner_rejects_invite_that_changes_nonce_or_recipient_binding() -> None:
    request, _private_key, bundle, _handoff = _case()
    seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
    changed_nonce = mint_invite_bundle(
        signer=seed,
        swarm_id="judge-swarm",
        seed_url="https://127.0.0.1:4443",
        ttl_seconds=120,
        nonce="different-nonce",
        issued_at=NOW,
    )
    with pytest.raises(PairingError, match="pairing_invite_binding_invalid"):
        approve_pairing_request(
            request=request,
            invite_bundle=changed_nonce,
            quota=QUOTA,
            now=NOW,
        )

    changed_request = copy.deepcopy(request)
    changed_request["peer_class"] = "linux_numpy_iroh"
    with pytest.raises(PairingError, match="pairing_request_signature_invalid"):
        approve_pairing_request(
            request=changed_request,
            invite_bundle=bundle,
            quota=QUOTA,
            now=NOW,
        )
