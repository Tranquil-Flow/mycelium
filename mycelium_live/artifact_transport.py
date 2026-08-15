"""Authenticated HTTPS transport for assignment-bound swarm artifact chunks."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
import copy
import fcntl
import hashlib
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import Ed25519EvidenceSigner
from mycelium_swarm_artifacts import (
    CHUNK_RECEIPT_PROTOCOL,
    CHUNK_REQUEST_PROTOCOL,
    SwarmArtifactContractError,
    sign_chunk_receipt,
    sign_chunk_request,
    validate_availability,
    validate_chunk_receipt,
    validate_chunk_request,
    validate_stage_pack_manifest,
)


_READ_PATH = "/v1/chunks/read"
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RECEIPT_HEADER_BYTES = 12 * 1024
_REQUEST_CLOCK_SKEW_MS = 5_000
_STREAM_BLOCK_BYTES = 64 * 1024


class ArtifactTransportError(RuntimeError):
    """Fail-closed transport error with a stable public reason code."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class _SharedResponseRateLimiter:
    """Pace bytes before they cross the source socket, across all streams."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_available = 0.0

    def wait(self, byte_count: int, *, maximum_bytes_per_second: int) -> None:
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_available)
            self._next_available = (
                scheduled + byte_count / maximum_bytes_per_second
            )
        delay = scheduled - now
        if delay > 0:
            time.sleep(delay)


def _canonical(value: object) -> bytes:
    return canonical_json_bytes(value)


def _private_directory(path: Path, code: str, *, create: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ArtifactTransportError(code)
    if create:
        candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ArtifactTransportError(code) from exc
    if (
        resolved != candidate
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ArtifactTransportError(code)
    return candidate


def _atomic_json(destination: Path, value: object) -> None:
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _bytes_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class ArtifactRequestReplayStore:
    """Durable bounded replay fence for successful source authorizations."""

    def __init__(self, root: Path, *, maximum_entries: int = 100_000) -> None:
        if type(maximum_entries) is not int or not 1 <= maximum_entries <= 1_000_000:
            raise ValueError("maximum_entries must be between 1 and 1000000")
        self.root = _private_directory(root, "artifact_replay_root_unsafe", create=True)
        self.maximum_entries = maximum_entries
        self._path = self.root / "requests.json"
        self._lock_path = self.root / "requests.lock"
        self._lock = threading.RLock()

    def _read(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        try:
            document = json.loads(self._path.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactTransportError("artifact_replay_state_corrupt") from exc
        if (
            not isinstance(document, dict)
            or len(document) > self.maximum_entries
            or not all(
                isinstance(key, str)
                and len(key) == 64
                and all(character in "0123456789abcdef" for character in key)
                and type(expires) is int
                and expires > 0
                for key, expires in document.items()
            )
        ):
            raise ArtifactTransportError("artifact_replay_state_corrupt")
        return document

    def claim(self, request: Mapping[str, Any], *, now_unix_ms: int) -> None:
        grant = request.get("grant")
        if not isinstance(grant, Mapping):
            raise ArtifactTransportError("artifact_chunk_request_invalid")
        raw = "\0".join(
            str(value)
            for value in (
                grant.get("grant_id"),
                request.get("request_id"),
                request.get("request_nonce"),
            )
        ).encode()
        key = hashlib.sha256(raw).hexdigest()
        expires = request.get("expires_at_unix_ms")
        if type(expires) is not int:
            raise ArtifactTransportError("artifact_chunk_request_invalid")
        with self._lock:
            descriptor = os.open(
                self._lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                entries = {
                    item: expiry
                    for item, expiry in self._read().items()
                    if expiry >= now_unix_ms
                }
                if key in entries:
                    raise ArtifactTransportError("artifact_chunk_request_replay")
                if len(entries) >= self.maximum_entries:
                    raise ArtifactTransportError("artifact_replay_store_full")
                entries[key] = expires
                _atomic_json(self._path, dict(sorted(entries.items())))
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)


Verifier = Callable[[bytes, dict[str, Any]], bool]
RecipientVerifierSource = Callable[[str, int], Verifier | None]


class ArtifactChunkSourceAuthority:
    """Validate current authority before returning any range from a verified chunk."""

    def __init__(
        self,
        *,
        source_member_id: str,
        source_membership_generation: int,
        object_root: Path,
        manifests: Mapping[str, Mapping[str, Any]],
        availabilities: Mapping[str, Mapping[str, Any]],
        source_signer: Ed25519EvidenceSigner,
        source_verifier: Verifier,
        provisioner_verifier: Verifier,
        recipient_verifier_source: RecipientVerifierSource,
        provisioner_generation: Callable[[], int],
        replay_store: ArtifactRequestReplayStore,
        clock_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        self.source_member_id = source_member_id
        self.source_membership_generation = source_membership_generation
        self.object_root = _private_directory(
            object_root, "artifact_source_root_unsafe"
        )
        self._authority_lock = threading.RLock()
        self.manifests: dict[str, dict[str, Any]] = {}
        self.availabilities: dict[str, dict[str, Any]] = {}
        self.source_signer = source_signer
        self.source_verifier = source_verifier
        self.provisioner_verifier = provisioner_verifier
        self.recipient_verifier_source = recipient_verifier_source
        self.provisioner_generation = provisioner_generation
        self.replay_store = replay_store
        self._clock = clock_unix_ms or (lambda: int(time.time() * 1_000))
        self.replace_authority(manifests, availabilities)

    def replace_authority(
        self,
        manifests: Mapping[str, Mapping[str, Any]],
        availabilities: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Atomically replace the source snapshot after complete validation."""

        validated_manifests = {
            digest: validate_stage_pack_manifest(manifest)
            for digest, manifest in manifests.items()
        }
        if (
            set(validated_manifests) != set(availabilities)
            or any(
                digest != manifest["manifest_digest"]
                for digest, manifest in validated_manifests.items()
            )
        ):
            raise ArtifactTransportError("artifact_source_authority_invalid")
        now = self._clock()
        validated_availabilities: dict[str, dict[str, Any]] = {}
        try:
            for digest, manifest in validated_manifests.items():
                validated_availabilities[digest] = validate_availability(
                    availabilities[digest],
                    verifier=self.source_verifier,
                    now_unix_ms=now,
                    expected_manifest_digest=manifest["manifest_digest"],
                    expected_membership_generation=self.source_membership_generation,
                )
        except (KeyError, SwarmArtifactContractError) as exc:
            raise ArtifactTransportError(
                "artifact_source_authority_invalid"
            ) from exc
        with self._authority_lock:
            self.manifests = copy.deepcopy(validated_manifests)
            self.availabilities = copy.deepcopy(validated_availabilities)

    def serve(self, request: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        now = self._clock()
        manifest_digest = request.get("manifest_digest")
        with self._authority_lock:
            manifest = copy.deepcopy(self.manifests.get(str(manifest_digest)))
            availability = copy.deepcopy(
                self.availabilities.get(str(manifest_digest))
            )
        if manifest is None or availability is None:
            raise ArtifactTransportError("artifact_manifest_unavailable")
        recipient = request.get("recipient_member_id")
        generation = request.get("recipient_membership_generation")
        if not isinstance(recipient, str) or type(generation) is not int:
            raise ArtifactTransportError("artifact_chunk_request_invalid")
        recipient_verifier = self.recipient_verifier_source(recipient, generation)
        if recipient_verifier is None:
            raise ArtifactTransportError("artifact_recipient_membership_stale")
        try:
            checked_availability = validate_availability(
                availability,
                verifier=self.source_verifier,
                now_unix_ms=now,
                expected_manifest_digest=manifest["manifest_digest"],
                expected_membership_generation=self.source_membership_generation,
            )
            checked_request = validate_chunk_request(
                request,
                provisioner_verifier=self.provisioner_verifier,
                recipient_verifier=recipient_verifier,
                now_unix_ms=now,
                expected_source_member_id=self.source_member_id,
                expected_manifest=manifest,
                expected_provisioner_generation=self.provisioner_generation(),
            )
        except SwarmArtifactContractError as exc:
            raise ArtifactTransportError(exc.code) from exc
        if (
            checked_request["chunk_digest"]
            not in checked_availability["available_chunk_digests"]
        ):
            raise ArtifactTransportError("artifact_chunk_not_advertised")
        self.replay_store.claim(checked_request, now_unix_ms=now)
        chunk = next(
            item
            for item in manifest["chunks"]
            if item["content_digest"] == checked_request["chunk_digest"]
        )
        path = self.object_root / checked_request["chunk_digest"].removeprefix(
            "sha256:"
        )
        try:
            if (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size != chunk["size_bytes"]
                or _sha256(path) != checked_request["chunk_digest"]
            ):
                raise ArtifactTransportError("artifact_source_chunk_unverified")
            with path.open("rb") as handle:
                handle.seek(checked_request["offset_bytes"])
                payload = handle.read(checked_request["length_bytes"])
        except ArtifactTransportError:
            raise
        except OSError as exc:
            raise ArtifactTransportError(
                "artifact_source_disappeared", retryable=True
            ) from exc
        if len(payload) != checked_request["length_bytes"]:
            raise ArtifactTransportError("artifact_source_disappeared", retryable=True)
        receipt = sign_chunk_receipt(
            {
                "protocol": CHUNK_RECEIPT_PROTOCOL,
                "request_id": checked_request["request_id"],
                "source_member_id": self.source_member_id,
                "source_membership_generation": self.source_membership_generation,
                "recipient_member_id": recipient,
                "manifest_digest": manifest["manifest_digest"],
                "chunk_digest": checked_request["chunk_digest"],
                "offset_bytes": checked_request["offset_bytes"],
                "length_bytes": checked_request["length_bytes"],
                "range_content_digest": _bytes_digest(payload),
                "advertisement_id": checked_availability["advertisement_id"],
                "responded_at_unix_ms": now,
            },
            self.source_signer,
        )
        return payload, receipt


class ArtifactChunkHTTPServer(ThreadingHTTPServer):
    authority: ArtifactChunkSourceAuthority
    maximum_bytes_per_second: int | None
    response_rate_limiter: _SharedResponseRateLimiter


def create_artifact_chunk_server(
    *,
    host: str,
    port: int,
    authority: ArtifactChunkSourceAuthority,
    tls_context: ssl.SSLContext | None,
    allow_insecure_loopback: bool = False,
    maximum_bytes_per_second: int | None = None,
) -> ArtifactChunkHTTPServer:
    try:
        loopback = host == "::1" or socket.gethostbyname(host).startswith("127.")
    except socket.gaierror as exc:
        raise ArtifactTransportError("artifact_bind_host_invalid") from exc
    if tls_context is None and not (allow_insecure_loopback and loopback):
        raise ArtifactTransportError("artifact_tls_required")
    if maximum_bytes_per_second is not None and (
        type(maximum_bytes_per_second) is not int
        or maximum_bytes_per_second < 1
    ):
        raise ArtifactTransportError("artifact_source_rate_invalid")

    class Handler(BaseHTTPRequestHandler):
        server: ArtifactChunkHTTPServer

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _error(self, status: int, code: str) -> None:
            body = _canonical({"error": code})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != _READ_PATH:
                self._error(404, "artifact_endpoint_not_found")
                return
            content_type = self.headers.get("Content-Type", "").partition(";")[0]
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if (
                content_type != "application/json"
                or not 1 <= length <= _MAX_REQUEST_BYTES
            ):
                self._error(400, "artifact_request_envelope_invalid")
                return
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError
                document = json.loads(raw)
                if not isinstance(document, dict):
                    raise ValueError
                payload, receipt = self.server.authority.serve(document)
                encoded_receipt = base64.urlsafe_b64encode(_canonical(receipt)).decode(
                    "ascii"
                )
                if len(encoded_receipt) > _MAX_RECEIPT_HEADER_BYTES:
                    raise ArtifactTransportError("artifact_receipt_too_large")
            except (UnicodeError, json.JSONDecodeError, ValueError):
                self._error(400, "artifact_request_envelope_invalid")
                return
            except ArtifactTransportError as exc:
                status = 409 if exc.code == "artifact_chunk_request_replay" else 403
                self._error(status, exc.code)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Mycelium-Artifact-Receipt", encoded_receipt)
            self.end_headers()
            grant = document.get("grant")
            grant_rate = (
                grant.get("maximum_bytes_per_second")
                if isinstance(grant, Mapping)
                else None
            )
            configured_rate = self.server.maximum_bytes_per_second
            rates = [
                rate
                for rate in (configured_rate, grant_rate)
                if type(rate) is int and rate > 0
            ]
            rate = min(rates) if rates else None
            for offset in range(0, len(payload), _STREAM_BLOCK_BYTES):
                block = payload[offset : offset + _STREAM_BLOCK_BYTES]
                if rate is not None:
                    self.server.response_rate_limiter.wait(
                        len(block), maximum_bytes_per_second=rate
                    )
                self.wfile.write(block)

    server = ArtifactChunkHTTPServer((host, port), Handler)
    server.authority = authority
    server.maximum_bytes_per_second = maximum_bytes_per_second
    server.response_rate_limiter = _SharedResponseRateLimiter()
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    return server


class ArtifactHTTPSChunkReader:
    """Provisioner reader that verifies TLS, source receipt, and returned range."""

    def __init__(
        self,
        *,
        endpoints: Mapping[str, str],
        availabilities: Mapping[str, Mapping[str, Any]],
        source_verifiers: Mapping[str, Verifier],
        recipient_member_id: str,
        recipient_membership_generation: int,
        recipient_signer: Ed25519EvidenceSigner,
        tls_context: ssl.SSLContext,
        clock_unix_ms: Callable[[], int] | None = None,
        timeout_seconds: float = 30.0,
        allow_insecure_loopback: bool = False,
    ) -> None:
        self.endpoints = dict(endpoints)
        self.availabilities = copy.deepcopy(dict(availabilities))
        self.source_verifiers = dict(source_verifiers)
        self.recipient_member_id = recipient_member_id
        self.recipient_membership_generation = recipient_membership_generation
        self.recipient_signer = recipient_signer
        self.tls_context = tls_context
        self._clock = clock_unix_ms or (lambda: int(time.time() * 1_000))
        self.timeout_seconds = timeout_seconds
        self.allow_insecure_loopback = allow_insecure_loopback

    def __call__(
        self,
        source: str,
        digest: str,
        offset: int,
        length: int,
        grant: Mapping[str, Any],
    ):
        endpoint = self.endpoints.get(source)
        availability = self.availabilities.get(source)
        source_verifier = self.source_verifiers.get(source)
        if endpoint is None or availability is None or source_verifier is None:
            raise ArtifactTransportError("artifact_source_transport_unavailable")
        parsed = urlsplit(endpoint)
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.hostname is None
            or parsed.port is None
            or parsed.scheme not in {"https", "http"}
            or (
                parsed.scheme == "http"
                and not (self.allow_insecure_loopback and loopback)
            )
        ):
            raise ArtifactTransportError("artifact_source_endpoint_invalid")
        now = self._clock()
        grant_expiry = grant.get("expires_at_unix_ms")
        if type(grant_expiry) is not int:
            raise ArtifactTransportError("artifact_grant_invalid")
        expires = min(grant_expiry, now + 30_000)
        issued = max(1, now - _REQUEST_CLOCK_SKEW_MS)
        request = sign_chunk_request(
            {
                "protocol": CHUNK_REQUEST_PROTOCOL,
                "request_id": "request-" + uuid.uuid4().hex,
                "request_nonce": "nonce-" + uuid.uuid4().hex,
                "grant": copy.deepcopy(dict(grant)),
                "source_member_id": source,
                "recipient_member_id": self.recipient_member_id,
                "recipient_membership_generation": self.recipient_membership_generation,
                "manifest_digest": grant.get("manifest_digest"),
                "chunk_digest": digest,
                "offset_bytes": offset,
                "length_bytes": length,
                "issued_at_unix_ms": issued,
                "expires_at_unix_ms": expires,
            },
            self.recipient_signer,
        )
        connection: HTTPConnection
        if parsed.scheme == "https":
            connection = HTTPSConnection(
                parsed.hostname,
                parsed.port,
                context=self.tls_context,
                timeout=self.timeout_seconds,
            )
        else:
            connection = HTTPConnection(
                parsed.hostname, parsed.port, timeout=self.timeout_seconds
            )
        try:
            body = _canonical(request)
            connection.request(
                "POST",
                _READ_PATH,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = response.read(length + 1)
            if response.status != 200:
                try:
                    error = json.loads(payload).get("error")
                except (AttributeError, UnicodeError, json.JSONDecodeError):
                    error = None
                raise ArtifactTransportError(
                    error if isinstance(error, str) else "artifact_source_rejected",
                    retryable=response.status >= 500,
                )
            if len(payload) != length:
                raise ArtifactTransportError(
                    "artifact_source_response_invalid", retryable=True
                )
            encoded_receipt = response.getheader("X-Mycelium-Artifact-Receipt")
            if not isinstance(encoded_receipt, str) or not encoded_receipt:
                raise ArtifactTransportError("artifact_source_receipt_missing")
            try:
                receipt_bytes = base64.b64decode(
                    encoded_receipt, altchars=b"-_", validate=True
                )
                if len(receipt_bytes) > _MAX_RECEIPT_HEADER_BYTES:
                    raise ValueError
                receipt = json.loads(receipt_bytes)
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                raise ArtifactTransportError("artifact_source_receipt_invalid") from exc
            try:
                validate_chunk_receipt(
                    receipt,
                    source_verifier=source_verifier,
                    request=request,
                    availability=availability,
                    returned_bytes=payload,
                )
            except SwarmArtifactContractError as exc:
                raise ArtifactTransportError(exc.code) from exc
        except ArtifactTransportError:
            raise
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            raise ArtifactTransportError(
                "artifact_source_disappeared", retryable=True
            ) from exc
        finally:
            connection.close()
        yield payload


__all__ = [
    "ArtifactChunkHTTPServer",
    "ArtifactChunkSourceAuthority",
    "ArtifactHTTPSChunkReader",
    "ArtifactRequestReplayStore",
    "ArtifactTransportError",
    "create_artifact_chunk_server",
]
