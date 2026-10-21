# SPDX-License-Identifier: AGPL-3.0-only
"""Regenerate the Python-authored interop fixtures for mycelium-mobile-core.

Run from the repository root:
    python3 -B native/mobile_core/tests/fixtures/generate_fixtures.py
    python3 -B native/mobile_core/tests/fixtures/generate_fixtures.py route_assignment
    python3 -B native/mobile_core/tests/fixtures/generate_fixtures.py stage_manifest_v2
    python3 -B native/mobile_core/tests/fixtures/generate_fixtures.py artifact_grant

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
from mycelium_mobile import pixel_stage  # noqa: E402
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


def stage_fixture() -> None:
    import random

    rng = random.Random(20260927)
    hidden, inner, layer = 8, 32, 1

    def vec(n: int) -> list[float]:
        return [round(rng.uniform(-0.5, 0.5), 6) for _ in range(n)]

    def mat(r: int, c: int) -> list[list[float]]:
        return [vec(c) for _ in range(r)]

    prefix = f"transformer.h.{layer}."
    tensors = {
        prefix + "ln_1.weight": [1.0 + x for x in vec(hidden)],
        prefix + "ln_1.bias": vec(hidden),
        prefix + "attn.c_attn.weight": mat(hidden, 3 * hidden),
        prefix + "attn.c_attn.bias": vec(3 * hidden),
        prefix + "attn.c_proj.weight": mat(hidden, hidden),
        prefix + "attn.c_proj.bias": vec(hidden),
        prefix + "ln_2.weight": [1.0 + x for x in vec(hidden)],
        prefix + "ln_2.bias": vec(hidden),
        prefix + "mlp.c_fc.weight": mat(hidden, inner),
        prefix + "mlp.c_fc.bias": vec(inner),
        prefix + "mlp.c_proj.weight": mat(inner, hidden),
        prefix + "mlp.c_proj.bias": vec(hidden),
    }
    digest = "sha256:" + "a" * 64
    pack = pixel_stage.build_stage_pack(
        run_id="run-fixture",
        deployment_id="deployment-fixture",
        assignment_id="assignment-fixture",
        stage_id="stage-1",
        model_id="local/tiny-gpt2-qualification",
        resolved_commit="fixture",
        manifest_digest=digest,
        parent_assignment_digest=digest,
        parent_load_proof_digest=digest,
        start_layer=layer,
        end_layer_exclusive=layer + 1,
        n_head=2,
        hidden_size=hidden,
        epsilon=1e-05,
        activation_function="gelu_new",
        scale_attn_weights=True,
        scale_attn_by_inverse_layer_idx=False,
        reorder_and_upcast_attn=False,
        add_cross_attention=False,
        tensors=tensors,
    )
    stage = pixel_stage.PixelStage.from_document(pack)
    rows = [vec(hidden) for _ in range(5)]
    output = stage.execute(
        request_id="req-1", assignment_id="assignment-fixture", stage_id="stage-1", hidden=rows
    )
    write("gpt2_stage.json", {"pack": pack, "hidden": rows, "output": output})


def route_assignment_fixture(
    pack: dict | None = None, name: str = "route_assignment.json", artifact_sources: list | None = None
) -> dict:
    """A real seed's answer to a member's signed route-assignment request."""

    import sqlite3
    import tempfile

    from mycelium_seed.route_assignment import (
        ROUTE_ASSIGNMENT_REQUEST_PROTOCOL,
        RouteAssignmentService,
        issue_route_assignment,
    )
    from mycelium_seed.state import SqliteSeedState

    seed = signer(0x22, "seed-endpoint-1")
    member = signer(0x11, "a" * 64)
    peer = "0d7550754e0800a5d237eef5826035766b9b3e5a15868a940ab289958788e3b0"
    if pack is None:
        pack = json.loads((OUT / "gpt2_stage.json").read_text())["pack"]
    capability = {
        "runtime_backend": "mobile-rust-reference",
        "transport": "iroh",
        "activation_protocol": "mycelium.router_wire.v1",
    }
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp).resolve() / "state.sqlite3"
        SqliteSeedState(database)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO seed_members VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "node-phone-1", member.endpoint_id, json.dumps([]),
                    "android_native_iroh", json.dumps(capability),
                    member.verification_key_digest, "inc-fixture-2", 3,
                    NOW + 300, 1, NOW, NOW + 30, None, 0, "RUNNING",
                ),
            )
        issue_route_assignment(
            database,
            node_id="node-phone-1",
            stage_pack=pack,
            peer_endpoint_id=peer,
            route_generation=7,
            now=lambda: NOW,
            id_source=lambda: "route-fixture-1",
            seed_key_record=seed.public_key_record(),
            artifact_sources=artifact_sources or (),
        )
        request = {
            "protocol": ROUTE_ASSIGNMENT_REQUEST_PROTOCOL,
            "message_id": "route-request-fixture-1",
            "swarm_id": "swarm-fixture",
            "sender_node_id": "node-phone-1",
            "sender_endpoint_id": member.endpoint_id,
            "recipient_node_id": "seed-node-1",
            "incarnation": "inc-fixture-2",
            "generation": 3,
            "issued_at": NOW + 1,
            "expires_at": NOW + 61,
            "applied_assignment_id": None,
        }
        envelope = {
            "protocol": "mycelium.membership.signed_message.v1",
            "message": request,
            "signature": member.sign(request),
            "verification_key": member.public_key_record(),
        }
        delivery = RouteAssignmentService(
            database=database,
            signer=seed,
            swarm_id="swarm-fixture",
            seed_node_id="seed-node-1",
            incarnation="seed-inc-fixture",
            clock=lambda: NOW + 2,
            id_source=lambda: "route-delivery-fixture-1",
            grant_id_source=lambda: "grant-delivery-fixture-1",
        ).answer(envelope)
    document = {
            "seed_key": seed.public_key_record(),
            "delivery": delivery,
            "expected": {
                "swarm_id": "swarm-fixture",
                "seed_node_id": "seed-node-1",
                "node_id": "node-phone-1",
                "endpoint_id": member.endpoint_id,
                "generation": 3,
                "request_message_id": "route-request-fixture-1",
                "peer_endpoint_id": peer,
                "now": NOW + 3,
            },
        }
    write(name, document)
    return document


#: Host golden of the synthetic 2-layer stage over splitmix64_unit_f32_v1
#: (seed 42, seq 3), from `cargo run --release --example qwen2_stage_golden --
#: <synthetic snapshot> 0 2 42 3` (debug and release builds agree).
SYNTHETIC_GOLDEN_DIGEST = "sha256:7029985567357ec911c682d2e01d49a3111648e7a4b49770378dbcb804327603"


def stage_manifest_v2_fixture() -> None:
    """A seed-signed v2 manifest for the synthetic Qwen2 stage, its objects'
    source, a golden input vector, and a real seed's v2 route-assignment answer."""

    import base64
    import hashlib
    import tempfile

    from mycelium_mobile import qwen_stage_manifest as qm

    synthetic = json.loads((OUT / "qwen2_synthetic.json").read_text())
    seed = signer(0x22, "seed-endpoint-1")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "model.safetensors"
        path.write_bytes(base64.b64decode(synthetic["safetensors_b64"]))
        manifest = qm.build_stage_manifest_from_safetensors(
            path,
            {"model_type": "qwen2", "hidden_act": "silu", **synthetic["config"]},
            stage_id="stage-synthetic-0-2",
            model_id="synthetic/qwen2-tiny",
            resolved_commit="7ae557604adf67be50417f59c2c2f167def9a775",
            start_layer=0,
            end_layer_exclusive=2,
            golden={
                "protocol": qm.GOLDEN_PROTOCOL,
                "generator": qm.GOLDEN_GENERATOR,
                "seed": 42,
                "seq": 3,
                "output_digest": SYNTHETIC_GOLDEN_DIGEST,
            },
        )
    signed = qm.sign_stage_manifest(manifest, seed)
    golden_input = qm.golden_input_f32(42, 2, 8)
    write(
        "qwen2_stage_manifest_v2.json",
        {
            "manifest": manifest,
            "signed": signed,
            "seed_key": seed.public_key_record(),
            "canonical_ascii_sha256": "sha256:" + hashlib.sha256(qm.canonical_ascii(manifest)).hexdigest(),
            "golden_input": {
                "seed": 42,
                "seq": 2,
                "hidden": 8,
                "f32_le_b64": base64.b64encode(golden_input).decode("ascii"),
            },
        },
    )
    source = signer(0x33, "c" * 64)
    route_assignment_fixture(
        signed,
        "route_assignment_v2.json",
        [{"endpoint_id": source.endpoint_id, "verification_key_digest": source.verification_key_digest}],
    )


def artifact_grant_fixture() -> None:
    """A seed-signed artifact grant for the synthetic v2 manifest, the phone's
    signed chunk request for one range and the source's signed receipt."""

    import base64

    from mycelium_mobile import artifact_grant as ag

    seed = signer(0x22, "seed-endpoint-1")
    member = signer(0x11, "a" * 64)
    source = signer(0x33, "c" * 64)
    manifest = json.loads((OUT / "qwen2_stage_manifest_v2.json").read_text())["manifest"]
    now = 1_790_000_000_000
    grant = ag.issue_grant(
        seed,
        grant_id="grant-fixture-1",
        swarm_id="swarm-fixture",
        seed_node_id="seed-node-1",
        recipient_node_id="node-phone-1",
        recipient_endpoint_id=member.endpoint_id,
        recipient_key_digest=member.verification_key_digest,
        assignment_id="route-fixture-1",
        manifest=manifest,
        sources=[{"endpoint_id": source.endpoint_id, "verification_key_digest": source.verification_key_digest}],
        now_unix_ms=now,
    )
    tensor = manifest["tensors"][0]
    request = ag.sign_chunk_request(
        member,
        grant=grant,
        request_id="chunk-request-1",
        request_nonce="nonce-1",
        source_endpoint_id=source.endpoint_id,
        object_digest=tensor["sha256"],
        offset_bytes=16,
        length_bytes=32,
        now_unix_ms=now + 1_000,
    )
    message, checked = ag.verify_chunk_request(
        request,
        seed_key_record=seed.public_key_record(),
        source_endpoint_id=source.endpoint_id,
        source_key_digest=source.verification_key_digest,
        object_sizes={tensor["sha256"]: tensor["size_bytes"]},
        now_unix_ms=now + 1_000,
    )
    data = bytes((i * 7) % 256 for i in range(32))
    receipt = ag.sign_chunk_receipt(source, request=message, grant=checked, data=data, now_unix_ms=now + 2_000)
    write(
        "artifact_grant.json",
        {
            "seed_key": seed.public_key_record(),
            "member_key": member.public_key_record(),
            "source_key": source.public_key_record(),
            "now_unix_ms": now,
            "grant": grant,
            "request": request,
            "receipt": receipt,
            "data_b64": base64.b64encode(data).decode("ascii"),
        },
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["route_assignment"]:
        route_assignment_fixture()
    elif sys.argv[1:] == ["stage_manifest_v2"]:
        stage_manifest_v2_fixture()
    elif sys.argv[1:] == ["artifact_grant"]:
        artifact_grant_fixture()
    else:
        stage_fixture()
        main()
        route_assignment_fixture()
        stage_manifest_v2_fixture()
        artifact_grant_fixture()
