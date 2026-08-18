from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import ssl
import stat
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import pytest

from mycelium_live.artifact_transport import (
    _SharedResponseRateLimiter,
    ArtifactChunkSourceAuthority,
    ArtifactHTTPSChunkReader,
    ArtifactRequestReplayStore,
    ArtifactTransportError,
    create_artifact_chunk_server,
)
from mycelium_live.artifact_agent import (
    AGENT_CONFIG_PROTOCOL,
    AVAILABILITY_BUNDLE_PROTOCOL,
    _availability_snapshot,
    load_artifact_source_agent,
)
from mycelium_live.artifact_provisioner import (
    ArtifactAcquisitionStore,
    SwarmArtifactProvisioner,
)
from mycelium_live.member_artifact_provisioner import (
    MEMBER_ACQUISITION_JOB_PROTOCOL,
    MemberArtifactAcquisitionError,
    acquire_member_stage_pack,
)
from mycelium_qualification.signing import (
    build_ed25519_verifier,
    generate_ed25519_signer,
)
from mycelium_node.identity import load_node_signer, load_or_create_node_signer
from mycelium_swarm_artifacts import (
    AVAILABILITY_PROTOCOL,
    CHUNK_REQUEST_PROTOCOL,
    GRANT_PROTOCOL,
    MANIFEST_PROTOCOL,
    POLICY_PROTOCOL,
    canonical_digest,
    merkle_proofs,
    merkle_root,
    sign_availability,
    sign_chunk_request,
    sign_grant,
    validate_availability_bundle,
)


def test_shared_source_rate_limiter_reserves_bytes_before_the_next_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_values = iter((10.0, 10.0))
    sleeps: list[float] = []
    monkeypatch.setattr(
        "mycelium_live.artifact_transport.time.monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(
        "mycelium_live.artifact_transport.time.sleep", sleeps.append
    )
    limiter = _SharedResponseRateLimiter()

    limiter.wait(500, maximum_bytes_per_second=500)
    limiter.wait(250, maximum_bytes_per_second=500)

    assert sleeps == [1.0]


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _manifest(payload: bytes) -> dict:
    contents = [
        payload[index : index + 65_536] for index in range(0, len(payload), 65_536)
    ]
    digests = [_digest(content) for content in contents]
    proofs = merkle_proofs(digests)
    document = {
        "protocol": MANIFEST_PROTOCOL,
        "manifest_id": "manifest-transport-1",
        "manifest_digest": "sha256:" + "0" * 64,
        "model_id": "Qwen/Qwen3-8B",
        "model_revision": "a" * 40,
        "model_artifact_digest": "sha256:" + "b" * 64,
        "source_quantization": "bfloat16",
        "serving_dtype": "float32",
        "serving_quantization": "bfloat16",
        "representation_digest": "sha256:" + "c" * 64,
        "owner_decision_digest": "sha256:" + "d" * 64,
        "feasibility_digest": "sha256:" + "e" * 64,
        "evidence_generation": 7,
        "assignment_id": "assignment-1",
        "assignment_digest": "sha256:" + "1" * 64,
        "graph_digest": "sha256:" + "2" * 64,
        "recipient_member_id": "member-recipient",
        "recipient_membership_generation": 9,
        "placement_id": "placement-1",
        "stage_id": "stage-1",
        "layer_start": 10,
        "layer_end_exclusive": 12,
        "component_scope": ["transformer_layers"],
        "tensor_scope_digest": "sha256:" + "3" * 64,
        "pack_format": "mycelium.stage_pack_stream.v1",
        "files": [
            {
                "relative_path": "deployment/layers.safetensors",
                "components": ["transformer_layers"],
                "offset_bytes": 0,
                "size_bytes": len(payload),
                "content_digest": _digest(payload),
            }
        ],
        "stage_pack_digest": _digest(payload),
        "chunk_size_bytes": 65_536,
        "total_size_bytes": len(payload),
        "merkle_root": merkle_root(digests),
        "chunks": [
            {
                "index": index,
                "offset_bytes": index * 65_536,
                "size_bytes": len(content),
                "content_digest": digests[index],
                "merkle_proof": list(proofs[index]),
            }
            for index, content in enumerate(contents)
        ],
        "issued_at_unix_ms": 1_000,
        "expires_at_unix_ms": 2_000,
        "owner_provenance": "owner-approved-exact-representation",
    }
    document["manifest_digest"] = canonical_digest(
        {key: value for key, value in document.items() if key != "manifest_digest"}
    )
    return document


def _grant(
    manifest: dict,
    signer,
    *,
    sources: tuple[str, ...] = ("member-source",),
    grant_id: str = "grant-transport-1",
) -> dict:
    return sign_grant(
        {
            "protocol": GRANT_PROTOCOL,
            "grant_id": grant_id,
            "nonce": "grant-nonce-1",
            "provisioner_generation": 1,
            "recipient_member_id": "member-recipient",
            "recipient_membership_generation": 9,
            "manifest_digest": manifest["manifest_digest"],
            "assignment_digest": manifest["assignment_digest"],
            "representation_digest": manifest["representation_digest"],
            "feasibility_digest": manifest["feasibility_digest"],
            "allowed_chunk_digests": sorted(
                item["content_digest"] for item in manifest["chunks"]
            ),
            "maximum_total_bytes": manifest["total_size_bytes"],
            "maximum_concurrency": 2,
            "maximum_bytes_per_second": 1_000_000,
            "authorized_source_member_ids": sorted(sources),
            "origin_fallback_allowed": False,
            "issued_at_unix_ms": 1_100,
            "not_before_unix_ms": 1_200,
            "expires_at_unix_ms": 1_900,
        },
        signer,
    )


def _availability(
    manifest: dict,
    signer,
    *,
    source_member_id: str = "member-source",
    membership_generation: int = 4,
    chunks: list[str] | None = None,
) -> dict:
    available = chunks or [item["content_digest"] for item in manifest["chunks"]]
    return sign_availability(
        {
            "protocol": AVAILABILITY_PROTOCOL,
            "advertisement_id": "advertisement-" + source_member_id,
            "source_member_id": source_member_id,
            "membership_generation": membership_generation,
            "manifest_digest": manifest["manifest_digest"],
            "available_chunk_digests": sorted(available),
            "verified_bytes": sum(
                item["size_bytes"]
                for item in manifest["chunks"]
                if item["content_digest"] in available
            ),
            "max_concurrent_streams": 2,
            "max_bytes_per_second": 1_000_000,
            "serving_priority": 1,
            "transfer_health": "healthy",
            "observed_at_unix_ms": 1_300,
            "valid_until_unix_ms": 1_800,
        },
        signer,
    )


def _tls(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext, Path]:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mycelium test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.pem"
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server.key"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(ca_path))
    return server_context, client_context, ca_path


def _authority(tmp_path: Path, payload: bytes):
    manifest = _manifest(payload)
    provisioner = generate_ed25519_signer(endpoint_id="provisioner")
    recipient = generate_ed25519_signer(endpoint_id="recipient")
    source = generate_ed25519_signer(endpoint_id="source")
    objects = tmp_path / "objects"
    objects.mkdir(mode=0o700)
    (objects / _digest(payload).removeprefix("sha256:")).write_bytes(payload)
    availability = _availability(manifest, source)
    authority = ArtifactChunkSourceAuthority(
        source_member_id="member-source",
        source_membership_generation=4,
        object_root=objects,
        manifests={manifest["manifest_digest"]: manifest},
        availabilities={manifest["manifest_digest"]: availability},
        source_signer=source,
        source_verifier=build_ed25519_verifier([source.public_key_record()]),
        provisioner_verifier=build_ed25519_verifier([provisioner.public_key_record()]),
        recipient_verifier_source=lambda member, generation: (
            build_ed25519_verifier([recipient.public_key_record()])
            if (member, generation) == ("member-recipient", 9)
            else None
        ),
        provisioner_generation=lambda: 1,
        replay_store=ArtifactRequestReplayStore(tmp_path / "replays"),
        clock_unix_ms=lambda: 1_500,
    )
    return authority, manifest, availability, provisioner, recipient, source


def test_https_reader_authenticates_with_bounded_recipient_clock_lead(
    tmp_path: Path,
) -> None:
    payload = b"verified-stage-chunk" * 2_000
    authority, manifest, availability, provisioner, recipient, source = _authority(
        tmp_path, payload
    )
    server_tls, client_tls, _ca = _tls(tmp_path)
    server = create_artifact_chunk_server(
        host="127.0.0.1",
        port=0,
        authority=authority,
        tls_context=server_tls,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reader = ArtifactHTTPSChunkReader(
            endpoints={
                "member-source": f"https://localhost:{server.server_address[1]}"
            },
            availabilities={"member-source": availability},
            source_verifiers={
                "member-source": build_ed25519_verifier([source.public_key_record()])
            },
            recipient_member_id="member-recipient",
            recipient_membership_generation=9,
            recipient_signer=recipient,
            tls_context=client_tls,
            clock_unix_ms=lambda: 1_501,
        )
        returned = b"".join(
            reader(
                "member-source",
                manifest["chunks"][0]["content_digest"],
                7,
                1_337,
                _grant(manifest, provisioner),
            )
        )
        assert returned == payload[7 : 7 + 1_337]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_availability_lease_refresh_preserves_content_identity(tmp_path: Path) -> None:
    payload = b"stable-availability"
    manifest = _manifest(payload)
    objects = tmp_path / "objects"
    objects.mkdir()
    (objects / _digest(payload).removeprefix("sha256:")).write_bytes(payload)
    signer = generate_ed25519_signer(endpoint_id="stable-source")
    config = {
        "advertisement_ttl_seconds": 300,
        "max_concurrent_streams": 2,
        "max_bytes_per_second": 1_000_000,
        "serving_priority": 1,
        "transfer_health": "healthy",
    }

    first, _ = _availability_snapshot(
        manifests={manifest["manifest_digest"]: manifest},
        object_root=objects,
        source="member-stable",
        generation=7,
        signer=signer,
        config=config,
        now=1_000,
    )
    renewed, _ = _availability_snapshot(
        manifests={manifest["manifest_digest"]: manifest},
        object_root=objects,
        source="member-stable",
        generation=7,
        signer=signer,
        config=config,
        now=1_500,
    )

    first_ad = first[manifest["manifest_digest"]]
    renewed_ad = renewed[manifest["manifest_digest"]]
    assert first_ad["advertisement_id"] == renewed_ad["advertisement_id"]
    assert first_ad["observed_at_unix_ms"] != renewed_ad["observed_at_unix_ms"]


def test_source_rejects_replay_stale_member_and_unencrypted_nonloopback(
    tmp_path: Path,
) -> None:
    payload = b"chunk-payload"
    authority, manifest, _availability_record, provisioner, recipient, _source = (
        _authority(tmp_path, payload)
    )
    grant = _grant(manifest, provisioner)
    statement = {
        "protocol": CHUNK_REQUEST_PROTOCOL,
        "request_id": "request-replay-1",
        "request_nonce": "nonce-replay-1",
        "grant": grant,
        "source_member_id": "member-source",
        "recipient_member_id": "member-recipient",
        "recipient_membership_generation": 9,
        "manifest_digest": manifest["manifest_digest"],
        "chunk_digest": manifest["chunks"][0]["content_digest"],
        "offset_bytes": 0,
        "length_bytes": len(payload),
        "issued_at_unix_ms": 1_400,
        "expires_at_unix_ms": 1_700,
    }
    request = sign_chunk_request(statement, recipient)
    assert authority.serve(request)[0] == payload
    with pytest.raises(ArtifactTransportError, match="artifact_chunk_request_replay"):
        authority.serve(request)

    stale = sign_chunk_request(
        {
            **statement,
            "request_id": "request-stale-1",
            "recipient_membership_generation": 8,
        },
        recipient,
    )
    with pytest.raises(
        ArtifactTransportError, match="artifact_recipient_membership_stale"
    ):
        authority.serve(stale)

    with pytest.raises(ArtifactTransportError, match="artifact_tls_required"):
        create_artifact_chunk_server(
            host="0.0.0.0",
            port=0,
            authority=authority,
            tls_context=None,
        )


def test_https_reader_rejects_untrusted_server_certificate(tmp_path: Path) -> None:
    payload = b"verified"
    authority, manifest, availability, provisioner, recipient, source = _authority(
        tmp_path, payload
    )
    server_tls, _client_tls, _ca = _tls(tmp_path)
    server = create_artifact_chunk_server(
        host="127.0.0.1", port=0, authority=authority, tls_context=server_tls
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reader = ArtifactHTTPSChunkReader(
            endpoints={
                "member-source": f"https://localhost:{server.server_address[1]}"
            },
            availabilities={"member-source": availability},
            source_verifiers={
                "member-source": build_ed25519_verifier([source.public_key_record()])
            },
            recipient_member_id="member-recipient",
            recipient_membership_generation=9,
            recipient_signer=recipient,
            tls_context=ssl.create_default_context(),
            clock_unix_ms=lambda: 1_500,
        )
        with pytest.raises(ArtifactTransportError, match="artifact_source_disappeared"):
            b"".join(
                reader(
                    "member-source",
                    manifest["chunks"][0]["content_digest"],
                    0,
                    len(payload),
                    _grant(manifest, provisioner),
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_provisioner_acquires_one_pack_from_two_authenticated_https_members(
    tmp_path: Path,
) -> None:
    payload = b"a" * 65_536 + b"b" * 65_536 + b"tail"
    manifest = _manifest(payload)
    chunks = manifest["chunks"]
    provisioner_signer = generate_ed25519_signer(endpoint_id="provisioner")
    recipient = generate_ed25519_signer(endpoint_id="recipient")
    source_a = generate_ed25519_signer(endpoint_id="source-a")
    source_b = generate_ed25519_signer(endpoint_id="source-b")
    provisioner_verifier = build_ed25519_verifier(
        [provisioner_signer.public_key_record()]
    )
    recipient_verifier = build_ed25519_verifier([recipient.public_key_record()])
    source_signers = {"member-a": source_a, "member-b": source_b}
    availability = {
        "member-a": _availability(
            manifest,
            source_a,
            source_member_id="member-a",
            chunks=[chunks[0]["content_digest"], chunks[2]["content_digest"]],
        ),
        "member-b": _availability(
            manifest,
            source_b,
            source_member_id="member-b",
            chunks=[chunks[1]["content_digest"], chunks[2]["content_digest"]],
        ),
    }
    server_tls, client_tls, _ca = _tls(tmp_path)
    servers = []
    threads = []
    endpoints = {}
    for member_id, signer in source_signers.items():
        object_root = tmp_path / member_id / "objects"
        object_root.mkdir(parents=True, mode=0o700)
        advertised = set(availability[member_id]["available_chunk_digests"])
        for chunk in chunks:
            if chunk["content_digest"] not in advertised:
                continue
            content = payload[
                chunk["offset_bytes"] : chunk["offset_bytes"] + chunk["size_bytes"]
            ]
            (object_root / chunk["content_digest"].removeprefix("sha256:")).write_bytes(
                content
            )
        authority = ArtifactChunkSourceAuthority(
            source_member_id=member_id,
            source_membership_generation=4,
            object_root=object_root,
            manifests={manifest["manifest_digest"]: manifest},
            availabilities={manifest["manifest_digest"]: availability[member_id]},
            source_signer=signer,
            source_verifier=build_ed25519_verifier([signer.public_key_record()]),
            provisioner_verifier=provisioner_verifier,
            recipient_verifier_source=lambda requested, generation: (
                recipient_verifier
                if (requested, generation) == ("member-recipient", 9)
                else None
            ),
            provisioner_generation=lambda: 1,
            replay_store=ArtifactRequestReplayStore(tmp_path / member_id / "replays"),
            clock_unix_ms=lambda: 1_500,
        )
        server = create_artifact_chunk_server(
            host="127.0.0.1", port=0, authority=authority, tls_context=server_tls
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
        endpoints[member_id] = f"https://localhost:{server.server_address[1]}"
    try:
        reader = ArtifactHTTPSChunkReader(
            endpoints=endpoints,
            availabilities=availability,
            source_verifiers={
                member: build_ed25519_verifier([signer.public_key_record()])
                for member, signer in source_signers.items()
            },
            recipient_member_id="member-recipient",
            recipient_membership_generation=9,
            recipient_signer=recipient,
            tls_context=client_tls,
            clock_unix_ms=lambda: 1_500,
        )
        grant = _grant(
            manifest,
            provisioner_signer,
            sources=("member-a", "member-b"),
            grant_id="grant-multi-source-1",
        )
        policy = {
            "protocol": POLICY_PROTOCOL,
            "chunk_size_bytes": 65_536,
            "maximum_sources": 2,
            "per_source_concurrency": 1,
            "aggregate_concurrency": 2,
            "maximum_retries_per_chunk": 0,
            "maximum_source_rotations": 2,
            "partial_state_ttl_seconds": 3_600,
            "disk_reserve_bytes": 0,
            "per_source_bytes_per_second": 500_000,
            "aggregate_bytes_per_second": 1_000_000,
            "serving_traffic_reserve_ratio": 0.4,
            "multi_source_threshold_bytes": 65_536,
            "minimum_predicted_improvement_ratio": 0.2,
            "allow_redundant_hedging": False,
            "thermal_classes_allowed": ["nominal"],
            "power_classes_allowed": ["external_power"],
        }
        binding_fields = {
            "model_id",
            "model_revision",
            "representation_digest",
            "owner_decision_digest",
            "feasibility_digest",
            "evidence_generation",
            "assignment_id",
            "assignment_digest",
            "graph_digest",
            "recipient_member_id",
            "recipient_membership_generation",
            "placement_id",
            "stage_id",
            "layer_start",
            "layer_end_exclusive",
            "component_scope",
            "tensor_scope_digest",
        }
        result = SwarmArtifactProvisioner(
            ArtifactAcquisitionStore(tmp_path / "recipient-store"),
            clock_unix_ms=lambda: 1_500,
            disk_free_bytes=lambda _path: 10**9,
        ).acquire(
            manifest=manifest,
            expected_binding={field: manifest[field] for field in binding_fields},
            grant=grant,
            advertisements=list(availability.values()),
            policy=policy,
            reader=reader,
            predicted_improvement_ratio=0.5,
            serving_reserve_satisfied=True,
        )
        assert result["state"] == "ready"
        assert result["active_source_count"] == 0
        assert result["eligible_source_count"] == 2
        assert result["origin_bytes"] == 0
        contributions = [item["verified_bytes"] for item in result["sources"]]
        assert all(value > 0 for value in contributions)
        assert sum(contributions) == len(payload)
        promoted = (
            tmp_path
            / "recipient-store"
            / "promoted"
            / manifest["manifest_id"]
            / "files"
            / "deployment"
            / "layers.safetensors"
        )
        assert promoted.read_bytes() == payload
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def test_expired_source_manifest_is_rejected_before_object_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(b"expired-stage-pack")
    objects = tmp_path / "objects"
    objects.mkdir()

    def unexpected_digest(_path: Path) -> str:
        pytest.fail("expired manifest attempted to hash an object")

    monkeypatch.setattr("mycelium_live.artifact_agent._digest", unexpected_digest)
    availabilities, bundle = _availability_snapshot(
        manifests={manifest["manifest_digest"]: manifest},
        object_root=objects,
        source="member-generic",
        generation=4,
        signer=generate_ed25519_signer(endpoint_id="expired-source"),
        config={"advertisement_ttl_seconds": 300},
        now=2_001,
    )

    assert availabilities == {}
    assert bundle["advertisements"] == []


def test_generic_source_agent_loads_private_config_and_publishes_signed_availability(
    tmp_path: Path,
) -> None:
    payload = b"generic-member-stage-pack"
    manifest = _manifest(payload)
    provisioner = generate_ed25519_signer(endpoint_id="provisioner")
    recipient = generate_ed25519_signer(endpoint_id="recipient")
    _server_tls, client_tls, _ca = _tls(tmp_path)
    (tmp_path / "server.key").chmod(0o600)
    identity_root = tmp_path / "identity"
    identity_root.mkdir(mode=0o700)
    identity_file = identity_root / "node.key"
    load_or_create_node_signer(identity_file)
    objects = tmp_path / "agent-objects"
    objects.mkdir(mode=0o700)
    (objects / _digest(payload).removeprefix("sha256:")).write_bytes(payload)
    private_state = tmp_path / "agent-state"
    private_state.mkdir(mode=0o700)
    manifest_inbox = private_state / "manifest-inbox"
    manifest_inbox.mkdir(mode=0o700)
    manifest_path = manifest_inbox / (
        manifest["manifest_digest"].removeprefix("sha256:") + ".json"
    )
    manifest_path.write_bytes(_canonical(manifest))
    replay_root = private_state / "replay"
    output_file = private_state / "availability.json"
    config = {
        "protocol": AGENT_CONFIG_PROTOCOL,
        "source_member_id": "member-generic",
        "source_membership_generation": 4,
        "source_identity_key_file": str(identity_file),
        "object_root": str(objects),
        "manifest_inbox_directory": str(manifest_inbox),
        "provisioner_generation": 1,
        "provisioner_verification_keys": [provisioner.public_key_record()],
        "recipient_authorities": [
            {
                "member_id": "member-recipient",
                "membership_generation": 9,
                "verification_key": recipient.public_key_record(),
            }
        ],
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "tls_certificate_file": str(tmp_path / "server.pem"),
        "tls_private_key_file": str(tmp_path / "server.key"),
        "replay_state_root": str(replay_root),
        "availability_output_file": str(output_file),
        "advertisement_ttl_seconds": 1,
        "max_concurrent_streams": 2,
        "max_bytes_per_second": 1_000_000,
        "serving_priority": 1,
        "transfer_health": "healthy",
    }
    config_path = private_state / "config.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    agent = load_artifact_source_agent(config_path, now_unix_ms=1_500)
    agent.publish_availability()
    persisted = json.loads(output_file.read_text())
    assert persisted["protocol"] == AVAILABILITY_BUNDLE_PROTOCOL
    assert persisted["source_member_id"] == "member-generic"
    assert persisted["advertisements"][0]["verified_bytes"] == len(payload)
    assert stat.S_IMODE(output_file.stat().st_mode) == 0o600

    second_manifest = _manifest(b"different-stage-pack")
    (manifest_inbox / (
        second_manifest["manifest_digest"].removeprefix("sha256:") + ".json"
    )).write_bytes(_canonical(second_manifest))
    assert agent.reconcile() is True
    refreshed = json.loads(output_file.read_text())
    assert len(refreshed["advertisements"]) == 2
    assert {
        item["manifest_digest"]: item["verified_bytes"]
        for item in refreshed["advertisements"]
    }[second_manifest["manifest_digest"]] == 0
    prior_authority = dict(agent.availability_bundle)
    (manifest_inbox / "invalid.json").write_text("{}\n")
    with pytest.raises(ArtifactTransportError, match="artifact_manifest_invalid"):
        agent.reconcile()
    assert agent.availability_bundle == prior_authority
    (manifest_inbox / "invalid.json").unlink()

    expired_config = {
        **config,
        "availability_output_file": str(private_state / "expired-availability.json"),
    }
    expired_config_path = private_state / "expired-config.json"
    expired_config_path.write_text(json.dumps(expired_config))
    expired_config_path.chmod(0o600)
    expired_agent = load_artifact_source_agent(
        expired_config_path,
        now_unix_ms=2_500,
    )
    try:
        assert expired_agent.availability_bundle["advertisements"] == []
    finally:
        expired_agent.server.server_close()

    thread = threading.Thread(target=agent.server.serve_forever, daemon=True)
    thread.start()
    try:
        availability = persisted["advertisements"][0]
        source_signer = load_node_signer(
            identity_file, endpoint_id="artifact-source-member-generic"
        )
        assert validate_availability_bundle(
            persisted,
            verifier=build_ed25519_verifier([source_signer.public_key_record()]),
            now_unix_ms=1_500,
            expected_source_member_id="member-generic",
            expected_membership_generation=4,
        )["advertisements"] == persisted["advertisements"]
        reader = ArtifactHTTPSChunkReader(
            endpoints={
                "member-generic": f"https://localhost:{agent.server.server_address[1]}"
            },
            availabilities={"member-generic": availability},
            source_verifiers={
                "member-generic": build_ed25519_verifier(
                    [source_signer.public_key_record()]
                )
            },
            recipient_member_id="member-recipient",
            recipient_membership_generation=9,
            recipient_signer=recipient,
            tls_context=client_tls,
            clock_unix_ms=lambda: 1_500,
        )
        returned = b"".join(
            reader(
                "member-generic",
                manifest["chunks"][0]["content_digest"],
                0,
                len(payload),
                _grant(manifest, provisioner, sources=("member-generic",)),
            )
        )
        assert returned == payload
    finally:
        agent.server.shutdown()
        agent.server.server_close()
        thread.join(timeout=5)


def test_assigned_member_acquires_and_promotes_without_coordinator_origin(
    tmp_path: Path,
) -> None:
    clock_calls = 0

    def request_clock() -> int:
        nonlocal clock_calls
        clock_calls += 1
        return 1_500

    payload = b"recipient-side-stage-pack" * 4_000
    manifest = _manifest(payload)
    provisioner = generate_ed25519_signer(endpoint_id="provisioner")
    source = generate_ed25519_signer(endpoint_id="source")
    recipient_root = tmp_path / "recipient-identity"
    recipient_root.mkdir(mode=0o700)
    recipient_identity = recipient_root / "node.key"
    recipient = load_or_create_node_signer(
        recipient_identity,
        endpoint_id="artifact-recipient-member-recipient",
    )
    availability = _availability(manifest, source)
    bundle = {
        "protocol": AVAILABILITY_BUNDLE_PROTOCOL,
        "source_member_id": "member-source",
        "membership_generation": 4,
        "advertisements": [availability],
        "published_at_unix_ms": 1_300,
    }
    objects = tmp_path / "source-objects"
    objects.mkdir(mode=0o700)
    for chunk in manifest["chunks"]:
        start = chunk["offset_bytes"]
        end = start + chunk["size_bytes"]
        (objects / chunk["content_digest"].removeprefix("sha256:")).write_bytes(
            payload[start:end]
        )
    authority = ArtifactChunkSourceAuthority(
        source_member_id="member-source",
        source_membership_generation=4,
        object_root=objects,
        manifests={manifest["manifest_digest"]: manifest},
        availabilities={manifest["manifest_digest"]: availability},
        source_signer=source,
        source_verifier=build_ed25519_verifier([source.public_key_record()]),
        provisioner_verifier=build_ed25519_verifier(
            [provisioner.public_key_record()]
        ),
        recipient_verifier_source=lambda member, generation: (
            build_ed25519_verifier([recipient.public_key_record()])
            if (member, generation) == ("member-recipient", 9)
            else None
        ),
        provisioner_generation=lambda: 1,
        replay_store=ArtifactRequestReplayStore(tmp_path / "member-source-replays"),
        clock_unix_ms=lambda: 1_500,
    )
    server_tls, _client_tls, ca_file = _tls(tmp_path)
    server = create_artifact_chunk_server(
        host="127.0.0.1", port=0, authority=authority, tls_context=server_tls
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        private = tmp_path / "recipient-job"
        private.mkdir(mode=0o700)
        manifest_file = private / "manifest.json"
        binding_file = private / "binding.json"
        grant_file = private / "grant.json"
        availability_file = private / "availability.json"
        binding_fields = {
            "model_id",
            "model_revision",
            "representation_digest",
            "owner_decision_digest",
            "feasibility_digest",
            "evidence_generation",
            "assignment_id",
            "assignment_digest",
            "graph_digest",
            "recipient_member_id",
            "recipient_membership_generation",
            "placement_id",
            "stage_id",
            "layer_start",
            "layer_end_exclusive",
            "component_scope",
            "tensor_scope_digest",
        }
        manifest_file.write_text(json.dumps(manifest))
        binding_file.write_text(
            json.dumps({field: manifest[field] for field in binding_fields})
        )
        grant_file.write_text(json.dumps(_grant(manifest, provisioner)))
        grant_file.chmod(0o600)
        availability_file.write_text(json.dumps(bundle))
        policy = {
            "protocol": POLICY_PROTOCOL,
            "chunk_size_bytes": 65_536,
            "maximum_sources": 2,
            "per_source_concurrency": 1,
            "aggregate_concurrency": 2,
            "maximum_retries_per_chunk": 0,
            "maximum_source_rotations": 2,
            "partial_state_ttl_seconds": 3_600,
            "disk_reserve_bytes": 0,
            "per_source_bytes_per_second": 500_000,
            "aggregate_bytes_per_second": 1_000_000,
            "serving_traffic_reserve_ratio": 0.4,
            "multi_source_threshold_bytes": 65_536,
            "minimum_predicted_improvement_ratio": 0.2,
            "allow_redundant_hedging": False,
            "thermal_classes_allowed": ["nominal"],
            "power_classes_allowed": ["external_power"],
        }
        job = {
            "protocol": MEMBER_ACQUISITION_JOB_PROTOCOL,
            "recipient_member_id": "member-recipient",
            "recipient_membership_generation": 9,
            "recipient_identity_key_file": str(recipient_identity),
            "provisioner_generation": 1,
            "provisioner_verification_keys": [provisioner.public_key_record()],
            "manifest_file": str(manifest_file),
            "expected_binding_file": str(binding_file),
            "grant_file": str(grant_file),
            "sources": [
                {
                    "member_id": "member-source",
                    "membership_generation": 4,
                    "endpoint": f"https://localhost:{server.server_address[1]}",
                    "verification_key": source.public_key_record(),
                    "availability_bundle_file": str(availability_file),
                }
            ],
            "tls_ca_file": str(ca_file),
            "artifact_store_root": str(tmp_path / "recipient-artifacts"),
            "policy": policy,
            "predicted_improvement_ratio": 0.5,
            "serving_reserve_satisfied": True,
            "status_output_file": str(private / "result.json"),
        }
        job_file = private / "job.json"
        job_file.write_text(json.dumps(job))
        job_file.chmod(0o600)

        result = acquire_member_stage_pack(job_file, clock_unix_ms=request_clock)

        assert result["state"] == "ready"
        assert clock_calls > 1
        assert result["origin_bytes"] == 0
        assert result["transferred_verified_bytes"] == len(payload)
        assert json.loads((private / "result.json").read_text())["state"] == "ready"
        promoted = (
            tmp_path
            / "recipient-artifacts"
            / "promoted"
            / manifest["manifest_id"]
            / "files"
            / "deployment"
            / "layers.safetensors"
        )
        assert promoted.read_bytes() == payload

        # Exact warm reacquisition is cache-only: the signed grant authorizes no
        # source and the member must prove the already verified content without
        # opening a transport or falling back to an origin.
        cache_grant = private / "cache-grant.json"
        cache_grant.write_text(
            json.dumps(
                _grant(
                    manifest,
                    provisioner,
                    sources=(),
                    grant_id="grant-cache-only-1",
                )
            )
        )
        cache_grant.chmod(0o600)
        cache_job = {
            **job,
            "grant_file": str(cache_grant),
            "sources": [],
            "status_output_file": str(private / "cache-result.json"),
        }
        job_file.write_text(json.dumps(cache_job))
        job_file.chmod(0o600)

        cache_result = acquire_member_stage_pack(
            job_file, clock_unix_ms=request_clock
        )

        assert cache_result["state"] == "ready"
        assert cache_result["cached_verified_bytes"] == len(payload)
        assert cache_result["transferred_verified_bytes"] == 0
        assert cache_result["origin_bytes"] == 0
        assert cache_result["missing_bytes"] == 0
        assert cache_result["quarantined_bytes"] == 0
        assert cache_result["sources"] == []

        stale = {**job, "recipient_membership_generation": 10}
        job_file.write_text(json.dumps(stale))
        job_file.chmod(0o600)
        with pytest.raises(
            MemberArtifactAcquisitionError,
            match="artifact_grant_ineligible",
        ):
            acquire_member_stage_pack(job_file, clock_unix_ms=lambda: 1_500)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
