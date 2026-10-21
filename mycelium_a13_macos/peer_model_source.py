# SPDX-License-Identifier: AGPL-3.0-only
"""Fetch pinned model bytes from the owner's Mac instead of Hugging Face.

Problem (two-Mac run 17): a joined Mac on a phone hotspot fetched 226 MB of a
1.5 GB snapshot from Hugging Face in 30 minutes while the two Macs' LAN path
does ~11.7 MB/s, and at a coworking space every joiner downloads separately.

The owner already holds the exact pinned snapshot (its acquisition verified
every byte against the pinned manifest).  This module lets a member read byte
ranges of THAT snapshot over the existing signed managed-control plane:

* owner: ``OwnerModelSource`` is one more route extension on the control
  mailbox (``/managed-control/model-source/read``).  A request is a member
  statement signed with the member's membership key and checked against the
  CURRENT member record (``_verify_target``), the member must still be live
  (a revocation is refused), the path must be one of the owner's verified
  manifest paths (a closed set, never a filesystem lookup by name), the file
  is opened component-by-component with ``O_NOFOLLOW`` and must be a regular,
  single-link, owner-owned file of exactly the pinned size, ranges are bounded
  (``MAX_CHUNK_BYTES``), concurrency is bounded (``MAX_CONCURRENT_READS`` total,
  ``MAX_CONCURRENT_PER_NODE`` per member) and every member has a served-byte
  budget.  Responses are owner-signed statements.
* reachability: the relay forwards ``/managed-control/*`` unchanged, so the
  route works relay-only (chunks of ``RELAY_CHUNK_BYTES`` keep every response
  frame under the relay's 1 MiB ``_MAX_FRAME``).  ``OwnerLanModelSource`` adds a
  LAN-direct HTTPS listener serving the SAME handler; its origin and its
  self-signed certificate travel inside the owner-signed run announcement, so
  the member verifies TLS against exactly that certificate (no Web PKI, no
  hostname-less verification, no TLS bypass).
* member: ``MultiSourceTransport`` implements the two interfaces the existing
  code already uses -- ``read_range`` (slice fetch) and ``resolve``/``open_file``
  (whole-snapshot acquisition) -- over an ordered source list (owner LAN,
  owner relay, Hugging Face).  It measures each source and probes the next one
  when the current one is slow, demotes a failing source, and ``reject`` lets
  the verifier quarantine a peer whose bytes failed a pinned digest so the
  bytes are fetched again from another source.

Trust: the peer is NEVER trusted for content.  The manifest still comes from
the pinned Hugging Face metadata (or an injected pinned manifest), slice
segments are verified against the owner-compiled plan digests, and every
whole file against its pinned digest, exactly as before.  A malicious owner can
only serve bytes that fail verification (-> quarantine and fallback, or
fail-closed when no other source remains).  Standing consent is unchanged: the
acquisition still counts every received byte against the same download budget
and storage cap; only the byte source changes.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

from mycelium_platform_neutral.offline import offline_enabled

from .acquisition import AcquisitionError, OfflineAcquisitionError, _validate_manifest
from .managed_control import (
    _BINDING_FIELDS,
    _REQUEST_COMMON_FIELDS,
    _RESPONSE_COMMON_FIELDS,
    ManagedControlError,
    _binding_from_member,
    _canonical_loads,
    _member_live,
    _NoRedirect,
)

READ = "mycelium.a13_model_source_read.v1"
RESPONSE = "mycelium.a13_model_source_bytes.v1"
HINT = "mycelium.a13_model_source_hint.v1"
MANIFEST_READ = "mycelium.a13_model_source_manifest_read.v1"
MANIFEST_RESPONSE = "mycelium.a13_model_source_manifest.v1"
PATH_READ = "/managed-control/model-source/read"
PATH_MANIFEST = "/managed-control/model-source/manifest"
DISCOVERY = "demo-run-discovery"
ENVIRONMENT = "MYCELIUM_A13_PEER_MODEL_SOURCE"  # on (default) | relay | off

#: Relay chunk: base64 inflates by 4/3, so 512 KiB of data is a ~700 KB signed
#: frame -- under the relay's 1 MiB ``public_relay._MAX_FRAME`` with headroom.
RELAY_CHUNK_BYTES = 512 * 1024
#: LAN chunk: ~2.8 MB frames, under managed control's 4 MiB ``MAX_FRAME_BYTES``.
LAN_CHUNK_BYTES = 2 * 1024 * 1024
MAX_CHUNK_BYTES = LAN_CHUNK_BYTES
#: Hugging Face fallback: one ranged request (slices) or one streamed read.
HF_CHUNK_BYTES = 8 * 1024 * 1024
HF_STREAM_CHUNK_BYTES = 1024 * 1024
MAX_CONCURRENT_READS = 4
MAX_CONCURRENT_PER_NODE = 2
#: A member may read at most this many times the snapshot per published
#: snapshot (a resumable re-fetch after a failure fits; a loop does not).
SERVE_BUDGET_FACTOR = 3
MAX_LAN_REQUEST_BYTES = 64 * 1024
MAX_LAN_CONNECTIONS = 8
LAN_TIMEOUT_SECONDS = 5.0
PEER_TIMEOUT_SECONDS = 20.0
_LAN_SOCKET_TIMEOUT_SECONDS = 15.0
_MAX_SEEN_REQUESTS = 65536
_MAX_PEM_CHARS = 4096
_REQ_FIELDS = frozenset(
    _REQUEST_COMMON_FIELDS | _BINDING_FIELDS
    | {"model_id", "resolved_commit", "path", "offset", "length"}
)
_RESP_FIELDS = frozenset(
    _RESPONSE_COMMON_FIELDS | _BINDING_FIELDS
    | {"model_id", "resolved_commit", "path", "offset", "length", "file_size",
       "chunk_sha256", "data_base64"}
)
_MANIFEST_REQ_FIELDS = frozenset(
    _REQUEST_COMMON_FIELDS | _BINDING_FIELDS | {"model_id", "resolved_commit"}
)
_MANIFEST_RESP_FIELDS = frozenset(
    _RESPONSE_COMMON_FIELDS | _BINDING_FIELDS | {"model_id", "resolved_commit", "manifest"}
)
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ModelSourceError(ManagedControlError, ValueError):
    """A closed model-source refusal (also a managed-control error code)."""


def _require(condition: object, code: str) -> None:
    if not condition:
        raise ModelSourceError(code)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ owner


def owner_snapshot(app: Any) -> tuple[Any, Path, dict[str, Any]] | None:
    """The owner app's verified, prepared snapshot (no re-hash), or ``None``."""
    acquisition = getattr(app, "acquisition", None)
    verified = getattr(acquisition, "verified_snapshot", None)
    if verified is None:
        return None
    value = verified()
    if value is None:
        return None
    root, manifest = value
    return acquisition.model, root, manifest


def _open_confined(root: Path, relative: str) -> int:
    """Open ``root/relative`` read-only without following any link."""
    parts = PurePosixPath(relative).parts
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    if os.open not in os.supports_dir_fd:  # pragma: no cover - win32
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise OSError("link")
        return os.open(current, flags | getattr(os, "O_BINARY", 0))
    directory = os.open(root, directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], flags, dir_fd=directory)
    finally:
        os.close(directory)


class OwnerModelSource:
    """Owner side: bounded, read-only ranges of the owner's verified snapshot."""

    def __init__(
        self,
        mailbox: Any,
        *,
        source: Callable[[], tuple[Any, Path, Mapping[str, Any]] | None],
        max_chunk_bytes: int = MAX_CHUNK_BYTES,
        max_concurrent: int = MAX_CONCURRENT_READS,
        max_concurrent_per_node: int = MAX_CONCURRENT_PER_NODE,
        budget_factor: int = SERVE_BUDGET_FACTOR,
    ) -> None:
        if (type(max_chunk_bytes) is not int or not 0 < max_chunk_bytes <= MAX_CHUNK_BYTES
                or type(max_concurrent) is not int or max_concurrent < 1
                or type(max_concurrent_per_node) is not int or max_concurrent_per_node < 1
                or type(budget_factor) is not int or budget_factor < 1 or not callable(source)):
            raise ValueError("model_source_configuration_invalid")
        self.mailbox = mailbox
        self._source = source
        self.max_chunk_bytes = max_chunk_bytes
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._per_node_limit = max_concurrent_per_node
        self._budget_factor = budget_factor
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}
        self._served: dict[tuple[str, str], int] = {}
        self._seen: dict[str, float] = {}  # request_id -> expires_at (replay window)
        self._index_key: tuple | None = None
        self._index: tuple[Any, Path, dict[str, dict[str, Any]], int, str] | None = None
        self.lan: OwnerLanModelSource | None = None

    def install(self) -> "OwnerModelSource":
        routes = dict(getattr(self.mailbox, "route_extensions", None) or {})
        routes[PATH_READ] = self.handle
        routes[PATH_MANIFEST] = self.handle_manifest
        self.mailbox.route_extensions = routes
        return self

    # -- snapshot ---------------------------------------------------------
    def snapshot(self) -> tuple[Any, Path, dict[str, dict[str, Any]], int, str] | None:
        try:
            value = self._source()
        except Exception:
            return None
        if value is None:
            return None
        model, root, manifest = value
        key = (model, str(root), json.dumps(manifest, sort_keys=True))
        with self._lock:
            if key != self._index_key:
                try:
                    checked = _validate_manifest(manifest, model=model)
                except AcquisitionError:
                    return None
                identity = _sha(json.dumps(checked, sort_keys=True).encode("utf-8"))
                self._index = (model, Path(root), {row["path"]: row for row in checked["files"]},
                               checked["total_bytes"], identity)
                self._index_key = key
            return self._index

    def available(self) -> bool:
        return self.snapshot() is not None

    def served_bytes(self, node_id: str) -> int:
        with self._lock:
            return sum(count for (node, _), count in self._served.items() if node == node_id)

    # -- the signed route -------------------------------------------------
    def handle(self, envelope: Any) -> dict[str, Any]:
        statement, member = self.mailbox._verify_target(envelope, protocol=READ, fields=_REQ_FIELDS)
        _require(statement["run_id"] == DISCOVERY and statement["deployment_id"] == DISCOVERY,
                 "model_source_request_invalid")
        # A revoked member keeps a verifiable binding (STOPPED); it gets no bytes.
        _require(_member_live(member), "model_source_member_not_live")
        self._admit(statement)
        path, offset, length = statement["path"], statement["offset"], statement["length"]
        _require(isinstance(statement["model_id"], str) and isinstance(statement["resolved_commit"], str)
                 and isinstance(path, str) and type(offset) is int and type(length) is int
                 and offset >= 0 and 0 < length <= self.max_chunk_bytes, "model_source_request_invalid")
        snapshot = self.snapshot()
        _require(snapshot is not None, "model_source_unavailable")
        model, root, index, total, identity = snapshot
        _require((model.model_id, model.resolved_commit)
                 == (statement["model_id"], statement["resolved_commit"]), "model_source_model_mismatch")
        record = index.get(path)
        # A closed set of verified manifest paths: never a lookup by caller name.
        _require(record is not None, "model_source_path_refused")
        size = record["size_bytes"]
        _require(offset + length <= size, "model_source_range_invalid")
        node = statement["target_node_id"]
        if not self._slots.acquire(blocking=False):
            raise ModelSourceError("model_source_busy")
        try:
            with self._lock:
                if self._active.get(node, 0) >= self._per_node_limit:
                    raise ModelSourceError("model_source_busy")
                budget_key = (node, identity)
                if self._served.get(budget_key, 0) + length > self._budget_factor * total:
                    raise ModelSourceError("model_source_budget_exhausted")
                self._active[node] = self._active.get(node, 0) + 1
            try:
                data = self._read(root, path, offset, length, size)
            finally:
                with self._lock:
                    self._active[node] -= 1
                    if not self._active[node]:
                        del self._active[node]
            with self._lock:
                self._served[budget_key] = self._served.get(budget_key, 0) + length
        finally:
            self._slots.release()
        return self.mailbox._response(RESPONSE, statement["request_id"], {
            **{key: statement[key] for key in _BINDING_FIELDS},
            "model_id": model.model_id, "resolved_commit": model.resolved_commit,
            "path": path, "offset": offset, "length": length, "file_size": size,
            "chunk_sha256": _sha(data), "data_base64": base64.b64encode(data).decode("ascii")})

    def _admit(self, statement: Mapping[str, Any]) -> None:
        # Reads carry no durable sequence; a captured request (the relay sees
        # control traffic) is refused on replay inside its freshness window.
        with self._lock:
            now = self.mailbox._clock()
            if len(self._seen) >= _MAX_SEEN_REQUESTS:
                self._seen = {key: until for key, until in self._seen.items() if until > now}
            _require(len(self._seen) < _MAX_SEEN_REQUESTS, "model_source_busy")
            _require(statement["request_id"] not in self._seen, "model_source_request_replayed")
            self._seen[statement["request_id"]] = statement["expires_at"]

    def handle_manifest(self, envelope: Any) -> dict[str, Any]:
        """Serve the pinned file list (paths, sizes, digests, commit) to a live member.

        The same checks as a byte read (signed member statement against the
        CURRENT member record, live member, replay refusal, exact model and
        commit).  The answer is the owner's already-verified manifest, owner
        signed; the member still pins the commit and digest-verifies every file.
        """
        statement, member = self.mailbox._verify_target(
            envelope, protocol=MANIFEST_READ, fields=_MANIFEST_REQ_FIELDS)
        _require(statement["run_id"] == DISCOVERY and statement["deployment_id"] == DISCOVERY,
                 "model_source_request_invalid")
        _require(_member_live(member), "model_source_member_not_live")
        self._admit(statement)
        _require(isinstance(statement["model_id"], str)
                 and isinstance(statement["resolved_commit"], str), "model_source_request_invalid")
        value = None
        try:
            value = self._source()
        except Exception:
            value = None
        _require(value is not None, "model_source_unavailable")
        model, _root, manifest = value
        snapshot = self.snapshot()
        _require(snapshot is not None, "model_source_unavailable")
        _require((model.model_id, model.resolved_commit)
                 == (statement["model_id"], statement["resolved_commit"]),
                 "model_source_model_mismatch")
        checked = _validate_manifest(manifest, model=model)
        return self.mailbox._response(MANIFEST_RESPONSE, statement["request_id"], {
            **{key: statement[key] for key in _BINDING_FIELDS},
            "model_id": model.model_id, "resolved_commit": model.resolved_commit,
            "manifest": checked})

    @staticmethod
    def _read(root: Path, path: str, offset: int, length: int, size: int) -> bytes:
        try:
            descriptor = _open_confined(root, path)
        except OSError:
            raise ModelSourceError("model_source_file_unsafe") from None
        try:
            metadata = os.fstat(descriptor)
            _require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
                     and metadata.st_size == size
                     and (os.name == "nt" or metadata.st_uid == os.geteuid()),
                     "model_source_file_unsafe")
            chunks = []
            done = 0
            while done < length:
                if hasattr(os, "pread"):
                    block = os.pread(descriptor, length - done, offset + done)
                else:  # pragma: no cover - win32 (one descriptor per request)
                    os.lseek(descriptor, offset + done, os.SEEK_SET)
                    block = os.read(descriptor, length - done)
                _require(block, "model_source_file_unsafe")
                chunks.append(block)
                done += len(block)
            return b"".join(chunks)
        except OSError:
            raise ModelSourceError("model_source_file_unsafe") from None
        finally:
            os.close(descriptor)

    # -- announcement hint ----------------------------------------------
    def hint(self) -> dict[str, Any] | None:
        """What the owner-signed announcement tells members (or ``None``)."""
        mode = os.environ.get(ENVIRONMENT, "on")
        if mode == "off" or not self.available():
            return None
        lan = None
        if mode == "on":
            lan = self._ensure_lan()
        return {"protocol": HINT, "relay": True,
                "lan_origin": None if lan is None else lan.origin,
                "tls_certificate_pem": None if lan is None else lan.certificate_pem}

    def _ensure_lan(self) -> "OwnerLanModelSource | None":
        with self._lock:
            if self.lan is not None:
                return self.lan
            if getattr(self, "_lan_failed", False):
                return None
        host = choose_lan_host()
        lan = None
        if host is not None:
            try:
                lan = OwnerLanModelSource(self, host=host).start()
            except Exception:
                lan = None
        with self._lock:
            if lan is None:
                self._lan_failed = True
            elif self.lan is None:
                self.lan = lan
                lan = None
            current = self.lan
        if lan is not None:
            lan.close()
        return current

    def close(self) -> None:
        with self._lock:
            lan, self.lan = self.lan, None
        if lan is not None:
            lan.close()


# -------------------------------------------------------------- LAN direct


def choose_lan_host() -> str | None:
    """This Mac's primary private LAN address (no packet is sent), or ``None``."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: route lookup only
        host = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.is_loopback or address.is_unspecified or not address.is_private:
        return None
    return host


def _self_signed(host: str) -> tuple[bytes, bytes]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mycelium owner model source")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=True, crl_sign=False,
                                     encipher_only=None, decipher_only=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


class _LanServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], source: OwnerModelSource) -> None:
        self.source = source
        self.slots = threading.BoundedSemaphore(MAX_LAN_CONNECTIONS)
        super().__init__(address, _LanHandler)

    def process_request(self, request, client_address):  # noqa: ANN001
        request.settimeout(_LAN_SOCKET_TIMEOUT_SECONDS)
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):  # noqa: ANN001
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):  # noqa: ANN001
        pass


class _LanHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _LanServer

    def log_message(self, *_args: Any) -> None:
        return

    def _send(self, status: int, document: Mapping[str, Any]) -> None:
        from mycelium_qualification.evidence import canonical_json_bytes  # as managed control
        raw = canonical_json_bytes(dict(document))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self._send(404, {"error": {"code": "model_source_route_unknown"}})

    def do_POST(self) -> None:  # noqa: N802
        try:
            _require(self.path in {PATH_READ, PATH_MANIFEST}, "model_source_route_unknown")
            lengths = self.headers.get_all("Content-Length", [])
            _require(self.headers.get("Content-Type") == "application/json"
                     and self.headers.get("Transfer-Encoding") is None
                     and self.headers.get("Cookie") is None
                     and self.headers.get("Authorization") is None
                     and len(lengths) == 1 and lengths[0].isdigit()
                     and 0 < int(lengths[0]) <= MAX_LAN_REQUEST_BYTES, "model_source_frame_invalid")
            body = _canonical_loads(self.rfile.read(int(lengths[0])))
            route = (self.server.source.handle_manifest if self.path == PATH_MANIFEST
                     else self.server.source.handle)
            self._send(200, route(body))
        except ManagedControlError as exc:
            status = 401 if exc.code in {"managed_control_signature_invalid",
                                         "managed_control_key_pin_mismatch",
                                         "managed_control_message_expired",
                                         "managed_control_message_from_future"} else (
                404 if exc.code == "model_source_route_unknown" else
                503 if exc.code == "model_source_busy" else 400)
            self._send(status, {"error": {"code": exc.code}})
        except BaseException:
            self._send(500, {"error": {"code": "model_source_internal_error"}})


class OwnerLanModelSource:
    """LAN-direct HTTPS listener for ``OwnerModelSource`` (same handler, same checks)."""

    def __init__(self, source: OwnerModelSource, *, host: str) -> None:
        address = ipaddress.ip_address(host)
        if not address.is_private or address.is_unspecified:
            raise ValueError("model_source_lan_host_invalid")
        certificate, key = _self_signed(host)
        self.certificate_pem = certificate.decode("ascii")
        server = _LanServer((host, 0), source)
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            import tempfile
            directory = Path(tempfile.mkdtemp(prefix="a13-lan-"))  # mode 0700, this user
            try:
                cert_path, key_path = directory / "cert.pem", directory / "key.pem"
                for path, body in ((cert_path, certificate), (key_path, key)):
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(body)
                context.load_cert_chain(str(cert_path), str(key_path))
            finally:
                for path in directory.iterdir():
                    path.unlink()
                directory.rmdir()
            server.socket = context.wrap_socket(server.socket, server_side=True)
        except BaseException:
            server.server_close()
            raise
        self._server = server
        self._thread: threading.Thread | None = None
        bracket = f"[{host}]" if address.version == 6 else host
        self.origin = f"https://{bracket}:{server.server_address[1]}"

    def start(self) -> "OwnerLanModelSource":
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            name="a13-model-source-lan", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(5)
            self._thread = None
        self._server.server_close()


def validate_hint(value: Any) -> dict[str, Any] | None:
    """A member's view of the owner-signed hint; anything unexpected -> ``None``."""
    if not isinstance(value, Mapping) or set(value) != {"protocol", "relay", "lan_origin",
                                                         "tls_certificate_pem"}:
        return None
    if value["protocol"] != HINT or type(value["relay"]) is not bool:
        return None
    origin, pem = value["lan_origin"], value["tls_certificate_pem"]
    if origin is None and pem is None:
        return dict(value)
    try:
        parts = urlsplit(origin)
        address = ipaddress.ip_address(parts.hostname)
        port = parts.port
    except (TypeError, ValueError):
        return {**value, "lan_origin": None, "tls_certificate_pem": None}
    if (parts.scheme != "https" or port is None or parts.path not in {"", "/"} or parts.query
            or parts.fragment or parts.username is not None or not address.is_private
            or address.is_unspecified or not isinstance(pem, str) or not 0 < len(pem) <= _MAX_PEM_CHARS
            or not pem.startswith("-----BEGIN CERTIFICATE-----")):
        return {**value, "lan_origin": None, "tls_certificate_pem": None}
    return dict(value)


# ----------------------------------------------------------------- member


class PeerRangeClient:
    """Member side of the signed route: one bounded, verified chunk per call."""

    def __init__(self, peer: Any, *, lan_origin: str | None = None,
                 tls_certificate_pem: str | None = None, timeout: float = PEER_TIMEOUT_SECONDS,
                 _test_ca_certificate: Path | None = None) -> None:
        if (lan_origin is None) != (tls_certificate_pem is None):
            raise ValueError("model_source_lan_configuration_invalid")
        self.peer = peer
        self.lan_origin = lan_origin
        self._pem = tls_certificate_pem
        self._timeout = timeout
        self._ca = _test_ca_certificate

    def _client(self) -> Any:
        client = self.peer.staging_control_client(_test_ca_certificate=self._ca)
        if self.lan_origin is None:
            return client
        from .managed_control import ManagedControlClient
        lan = ManagedControlClient(base_url=self.lan_origin, signer=client.signer,
                                   trusted_owner_key_record=client.trusted_owner_key_record,
                                   swarm_id=client.swarm_id, owner_node_id=client.owner_node_id,
                                   timeout=self._timeout)
        # TLS is verified against exactly the certificate the owner signed into
        # its announcement (an IP SAN for this LAN address), never Web PKI.
        context = ssl.create_default_context(cadata=self._pem)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        lan._opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=context))
        return lan

    def read_manifest(self, model_id: str, resolved_commit: str) -> dict[str, Any]:
        """The owner's pinned manifest for exactly this model and commit (not yet trusted)."""
        client = self._client()
        member = self.peer.staging_control_snapshot()
        binding = _binding_from_member(member, DISCOVERY, DISCOVERY)
        now = client._clock()
        request_id = uuid.uuid4().hex
        statement = {**binding, "protocol": MANIFEST_READ, "swarm_id": client.swarm_id,
                     "request_id": request_id, "sequence": 1, "nonce": uuid.uuid4().hex,
                     "issued_at": now, "expires_at": now + 30, "model_id": model_id,
                     "resolved_commit": resolved_commit}
        response = client._request(PATH_MANIFEST, client._signed_outbound(statement))
        result = client._verified_response(response, protocol=MANIFEST_RESPONSE,
                                           fields=_MANIFEST_RESP_FIELDS, request_id=request_id,
                                           target=member, run_id=DISCOVERY, deployment_id=DISCOVERY)
        _require(result["model_id"] == model_id and result["resolved_commit"] == resolved_commit
                 and isinstance(result["manifest"], Mapping), "model_source_response_invalid")
        return dict(result["manifest"])

    def read(self, model_id: str, resolved_commit: str, path: str, offset: int, length: int) -> bytes:
        client = self._client()
        member = self.peer.staging_control_snapshot()
        binding = _binding_from_member(member, DISCOVERY, DISCOVERY)
        now = client._clock()
        request_id = uuid.uuid4().hex
        statement = {**binding, "protocol": READ, "swarm_id": client.swarm_id,
                     "request_id": request_id, "sequence": 1, "nonce": uuid.uuid4().hex,
                     "issued_at": now, "expires_at": now + 30, "model_id": model_id,
                     "resolved_commit": resolved_commit, "path": path, "offset": offset,
                     "length": length}
        response = client._request(PATH_READ, client._signed_outbound(statement))
        result = client._verified_response(response, protocol=RESPONSE, fields=_RESP_FIELDS,
                                           request_id=request_id, target=member,
                                           run_id=DISCOVERY, deployment_id=DISCOVERY)
        _require(all(result[key] == statement[key]
                     for key in ("model_id", "resolved_commit", "path", "offset", "length"))
                 and isinstance(result["data_base64"], str)
                 and isinstance(result["chunk_sha256"], str), "model_source_response_invalid")
        try:
            data = base64.b64decode(result["data_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise ModelSourceError("model_source_response_invalid") from None
        # Transport integrity only; content trust comes from the pinned digests.
        _require(len(data) == length and _sha(data) == result["chunk_sha256"],
                 "model_source_response_invalid")
        return data


class _Source:
    def __init__(self, name: str, *, chunk_bytes: int, trusted: bool,
                 read: Callable[[str, str, str, int, int], bytes],
                 fatal: tuple[str, ...] = (),
                 read_manifest: Callable[[str, str], Mapping[str, Any]] | None = None) -> None:
        self.name = name
        self.fatal = frozenset(fatal)
        self.chunk_bytes = chunk_bytes
        self.trusted = trusted
        self._read = read
        self.read_manifest = read_manifest
        self.enabled = True
        self.strikes = 0
        self.rate: float | None = None
        self.bytes = 0
        self.since_probe = 0
        self.probed = False
        self.reason: str | None = None

    def read_range(self, model_id, commit, path, offset, length):  # noqa: ANN001
        return self._read(model_id, commit, path, offset, length)

    def view(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled, "bytes": self.bytes,
                "bytes_per_second": None if self.rate is None else int(self.rate),
                "reason": self.reason}


def peer_source(name: str, client: PeerRangeClient, *, chunk_bytes: int,
                fatal: tuple[str, ...] = ()) -> _Source:
    """``fatal``: extra codes that disable at once (an unreachable LAN path)."""
    return _Source(name, chunk_bytes=chunk_bytes, trusted=False, read=client.read, fatal=fatal,
                   read_manifest=getattr(client, "read_manifest", None))


class _HuggingFaceSource(_Source):
    """The pinned-revision origin (Web PKI): ranged reads, or one open stream."""

    def __init__(self, transport: Any) -> None:
        super().__init__("huggingface", chunk_bytes=HF_CHUNK_BYTES, trusted=True,
                         read=transport.read_range)
        self.transport = transport
        self._stream: Any = None
        self._stream_at: tuple[str, int] | None = None

    def close_stream(self) -> None:
        stream, self._stream, self._stream_at = self._stream, None, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def read_file(self, model: Any, record: Mapping[str, Any], position: int, length: int) -> bytes:
        if self._stream is None or self._stream_at != (record["path"], position):
            self.close_stream()
            response = self.transport.open_file(model, record, offset=position)
            size = record["size_bytes"]
            status = getattr(response, "status", None)
            expected = f"bytes {position}-{size - 1}/{size}"
            if (response.headers.get("Content-Encoding") not in {None, "identity"}
                    or not ((position == 0 and status == 200)
                            or (status == 206 and response.headers.get("Content-Range") == expected))):
                response.close()
                raise AcquisitionError("source_response_invalid")
            self._stream, self._stream_at = response, (record["path"], position)
        parts, done = [], 0
        try:
            while done < length:
                block = self._stream.read(min(64 * 1024, length - done))
                if not block:
                    raise AcquisitionError("source_disconnected")
                parts.append(block)
                done += len(block)
        except AcquisitionError:
            self.close_stream()
            raise
        except (OSError, http.client.HTTPException) as exc:
            self.close_stream()
            raise AcquisitionError("source_disconnected") from exc
        self._stream_at = (record["path"], position + done)
        return b"".join(parts)


_FATAL = frozenset({
    "managed_control_response_signature_invalid", "managed_control_response_invalid",
    "managed_control_route_unknown", "managed_control_key_pin_mismatch",
    "model_source_response_invalid", "model_source_unavailable", "model_source_model_mismatch",
    "model_source_path_refused", "model_source_budget_exhausted", "model_source_member_not_live",
    "model_source_request_invalid", "model_source_range_invalid", "model_source_file_unsafe",
    "model_source_request_replayed",
    "source_response_invalid", "model_manifest_commit_mismatch", "model_manifest_invalid",
})


class MultiSourceTransport:
    """``read_range`` + ``resolve``/``open_file`` over ordered byte sources.

    Order is preference (owner LAN, owner relay, Hugging Face).  A source that
    fails is struck (and disabled after ``max_strikes`` or at once for a
    protocol/identity failure); the chunk is retried on the next source.  When
    the current source has served ``probe_bytes`` since its last probe and is
    slower than ``slow_bytes_per_second``, the next chunk probes the next
    source, which moves ahead if it was ``switch_ratio`` times faster.
    """

    def __init__(self, sources: list[_Source], *, fallback: Any | None,
                 offline: bool = False, prefer_fallback: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 probe_bytes: int = 16 * 1024 * 1024,
                 slow_bytes_per_second: float = 4 * 1024 * 1024,
                 switch_ratio: float = 1.25, max_strikes: int = 3) -> None:
        self._order: list[_Source] = list(sources)
        self._fallback = fallback
        self._offline = offline
        self._hf: _HuggingFaceSource | None = None
        if fallback is not None:
            self._hf = _HuggingFaceSource(fallback)
            if prefer_fallback:
                # No owner LAN path: the pinned origin before the owner's uplink.
                self._order.insert(0, self._hf)
            else:
                self._order.append(self._hf)
        if not self._order:
            raise ValueError("model_source_sources_required")
        # Longest single read in flight (the acquisition's close() bound).
        self.timeout_seconds = max(float(getattr(fallback, "timeout_seconds", 30.0)),
                                   PEER_TIMEOUT_SECONDS)
        self._cancelled = threading.Event()
        self._clock = clock
        self._probe_bytes = probe_bytes
        self._slow = slow_bytes_per_second
        self._ratio = switch_ratio
        self._max_strikes = max_strikes
        self._lock = threading.RLock()
        self._probe: tuple[_Source, _Source] | None = None
        self._last_code: str | None = None
        self._file_sources: dict[str, set[str]] = {}
        self._range_sources: dict[tuple[str, int, int], set[str]] = {}
        self._resumed: set[str] = set()
        self._retried: set[str] = set()

    # -- trusted metadata stays on the pinned origin ---------------------
    def resolve(self, model: Any) -> dict[str, Any]:
        if self._fallback is not None:
            return self._fallback.resolve(model)
        return self._resolve_from_owner(model)

    def _resolve_from_owner(self, model: Any) -> dict[str, Any]:
        """No pinned origin: take the pinned file list from the owner, never trust it blindly.

        The manifest must name exactly the pinned model and commit; the file
        digests in it are then enforced on every received byte by the
        acquisition, as with a Hugging Face manifest.
        """
        last: str | None = None
        for source in list(self._order):
            reader = source.read_manifest
            if reader is None or not source.enabled:
                continue
            try:
                document = reader(model.model_id, model.resolved_commit)
                if (not isinstance(document, Mapping)
                        or document.get("model_id") != model.model_id
                        or document.get("resolved_commit") != model.resolved_commit):
                    raise AcquisitionError("model_manifest_commit_mismatch")
                return _validate_manifest(document, model=model)
            except Exception as error:
                self._failed(source, error)
                code = getattr(error, "code", None)
                last = code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "source_unavailable"
                if last in {"model_manifest_commit_mismatch", "model_manifest_invalid"}:
                    raise AcquisitionError(last) from None
        if last is not None:
            raise AcquisitionError(last)
        if self._offline:
            raise OfflineAcquisitionError("the pinned model metadata")
        raise AcquisitionError("source_unavailable")

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [source.view() for source in self._order]

    def cancel(self) -> None:
        """Stop before the next chunk (the acquisition's cancel/close)."""
        self._cancelled.set()

    def reset_cancel(self) -> None:
        self._cancelled.clear()

    # -- selection ----------------------------------------------------------
    def _pick(self) -> _Source:
        if self._cancelled.is_set():
            raise AcquisitionError("acquisition_cancelled")
        with self._lock:
            enabled = [source for source in self._order if source.enabled]
            if not enabled:
                code = self._last_code if self._last_code and _CODE_RE.fullmatch(self._last_code) else None
                raise AcquisitionError(code or "source_unavailable")
            if self._probe is not None and self._probe[1].enabled:
                return self._probe[1]
            self._probe = None
            return enabled[0]

    def _succeeded(self, source: _Source, count: int, seconds: float) -> None:
        with self._lock:
            rate = count / max(seconds, 1e-6)
            source.rate = rate if source.rate is None else 0.7 * source.rate + 0.3 * rate
            source.bytes += count
            source.since_probe += count
            source.strikes = 0
            probe = self._probe
            if probe is not None and probe[1] is source:
                self._probe = None
                base = probe[0]
                if base.enabled and base.rate is not None and source.rate > self._ratio * base.rate:
                    self._order.remove(source)
                    self._order.insert(self._order.index(base), source)
                return
            enabled = [item for item in self._order if item.enabled]
            if (enabled and enabled[0] is source and source.since_probe >= self._probe_bytes
                    and source.rate < self._slow):
                later = enabled[1:]
                candidates = [item for item in later if not item.probed]
                if not candidates and later:
                    for item in later:
                        item.probed = False
                    candidates = later
                if candidates:
                    candidates[0].probed = True
                    self._probe = (source, candidates[0])
                source.since_probe = 0

    def _failed(self, source: _Source, error: BaseException) -> None:
        code = getattr(error, "code", None)
        code = code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "source_unavailable"
        with self._lock:
            self._last_code = code
            if self._probe is not None and self._probe[1] is source:
                self._probe = None
            source.strikes += 1
            if code in _FATAL or code in source.fatal or source.strikes >= self._max_strikes:
                source.enabled = False
                source.reason = code

    def _timed(self, source: _Source, read: Callable[[], bytes], length: int) -> bytes | None:
        started = self._clock()
        try:
            data = read()
            if not isinstance(data, bytes) or len(data) != length:
                raise ModelSourceError("model_source_response_invalid")
        except Exception as error:  # every source failure is bounded and demoted
            self._failed(source, error)
            return None
        self._succeeded(source, length, self._clock() - started)
        return data

    # -- slice interface ----------------------------------------------------
    def read_range(self, model_id: str, resolved_commit: str, path: str, offset: int,
                   length: int) -> bytes:
        if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0:
            raise AcquisitionError("range_request_invalid")
        parts: list[bytes] = []
        used: set[str] = set()
        position, end = offset, offset + length
        while position < end:
            source = self._pick()
            count = min(source.chunk_bytes, end - position)
            data = self._timed(source, lambda: source.read_range(
                model_id, resolved_commit, path, position, count), count)
            if data is None:
                continue
            parts.append(data)
            used.add(source.name)
            position += count
        with self._lock:
            self._range_sources[(path, offset, length)] = used
        return b"".join(parts)

    # -- whole-file interface ------------------------------------------------
    def open_file(self, model: Any, record: Mapping[str, Any], *, offset: int) -> "_MultiSourceFile":
        with self._lock:
            if offset == 0:
                self._file_sources[record["path"]] = set()
                self._resumed.discard(record["path"])
            else:
                self._file_sources.setdefault(record["path"], set())
                self._resumed.add(record["path"])
        return _MultiSourceFile(self, model, record, offset)

    def reject(self, path: str, offset: int | None = None, length: int | None = None) -> bool:
        """Bytes from these sources failed a pinned digest: quarantine the peers.

        True when the verifier should fetch the bytes again (from what remains).
        A pinned-origin-only failure is never retried here: that is a real
        mismatch and stays fail-closed.
        """
        with self._lock:
            if offset is None:
                used = self._file_sources.get(path, set())
            else:
                used = self._range_sources.get((path, offset, length), set())
            quarantined = False
            for source in self._order:
                if source.name in used and not source.trusted and source.enabled:
                    source.enabled = False
                    source.reason = "digest_mismatch"
                    quarantined = True
            if not quarantined and offset is None and path in self._resumed and path not in self._retried:
                # The partial predates this attempt; its source is unknown.
                self._retried.add(path)
                quarantined = True
            return quarantined and any(source.enabled for source in self._order)


class _Headers(dict):
    def get_all(self, name: str, default: Any = None) -> Any:
        value = self.get(name)
        return default if value is None else [value]


class _MultiSourceFile:
    """A response-shaped reader the acquisition consumes like an HTTP body."""

    def __init__(self, owner: MultiSourceTransport, model: Any, record: Mapping[str, Any],
                 offset: int) -> None:
        size = record["size_bytes"]
        if type(offset) is not int or not 0 <= offset < size:
            raise AcquisitionError("range_request_invalid")
        self._owner = owner
        self._model = model
        self._record = dict(record)
        self._position = offset
        self._size = size
        self._buffer = b""
        self.status = 206 if offset else 200
        headers = {"Content-Length": str(size - offset)}
        if offset:
            headers["Content-Range"] = f"bytes {offset}-{size - 1}/{size}"
        self.headers = _Headers(headers)

    def _fill(self) -> None:
        owner, record = self._owner, self._record
        while not self._buffer and self._position < self._size:
            source = owner._pick()
            hf = owner._hf
            if hf is not None and source is not hf:
                hf.close_stream()
            if source is hf:
                count = min(HF_STREAM_CHUNK_BYTES, self._size - self._position)
                position = self._position
                data = owner._timed(source, lambda: hf.read_file(self._model, record, position, count),
                                    count)
            else:
                count = min(source.chunk_bytes, self._size - self._position)
                position = self._position
                data = owner._timed(source, lambda: source.read_range(
                    self._model.model_id, self._model.resolved_commit, record["path"], position, count),
                    count)
            if data is None:
                continue
            with owner._lock:
                owner._file_sources.setdefault(record["path"], set()).add(source.name)
            self._buffer = data
            self._position += count

    def read(self, amount: int = -1) -> bytes:
        if not self._buffer:
            self._fill()
        if amount is None or amount < 0:
            amount = len(self._buffer)
        data, self._buffer = self._buffer[:amount], self._buffer[amount:]
        return data

    def close(self) -> None:
        if self._owner._hf is not None:
            self._owner._hf.close_stream()

    def __enter__(self) -> "_MultiSourceFile":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def lan_reachable(owner_origin: str, joiner_host: str | None) -> bool:
    """True when this Mac is in the same IPv4 /24 as the owner's LAN listener.

    Anything else (other network, no private address, IPv6, unparsable) is
    ``False``: the private-IP dial would go nowhere or to an unrelated host.
    """
    if not joiner_host:
        return False
    try:
        owner = ipaddress.ip_address(urlsplit(owner_origin).hostname)
        joiner = ipaddress.ip_address(joiner_host)
    except (TypeError, ValueError):
        return False
    if owner.version != 4 or joiner.version != 4:
        return False
    return ipaddress.ip_network(f"{owner}/24", strict=False) == ipaddress.ip_network(
        f"{joiner}/24", strict=False)


def joiner_model_source(app: Any, announcement: Mapping[str, Any] | None, *,
                        fallback: Any | None = None,
                        _test_ca_certificate: Path | None = None) -> MultiSourceTransport | None:
    """The member's byte source for this announcement, or ``None`` (HF only, as before)."""
    if os.environ.get(ENVIRONMENT, "on") == "off" or not announcement:
        return None
    hint = validate_hint(announcement.get("model_source"))
    peer = getattr(getattr(app, "authority", None), "peer", None)
    if hint is None or peer is None:
        return None
    sources = []
    offered_lan = hint["lan_origin"] is not None
    on_owner_lan = offered_lan and lan_reachable(hint["lan_origin"], choose_lan_host())
    if on_owner_lan:
        # Same /24 as the owner; a firewall block still falls through at once.
        sources.append(peer_source("owner_lan", PeerRangeClient(
            peer, lan_origin=hint["lan_origin"], tls_certificate_pem=hint["tls_certificate_pem"],
            timeout=LAN_TIMEOUT_SECONDS, _test_ca_certificate=_test_ca_certificate),
            chunk_bytes=LAN_CHUNK_BYTES, fatal=("managed_control_unreachable",)))
    if hint["relay"]:
        sources.append(peer_source("owner_relay", PeerRangeClient(
            peer, _test_ca_certificate=_test_ca_certificate), chunk_bytes=RELAY_CHUNK_BYTES))
    if not sources:
        return None
    offline = offline_enabled()
    if fallback is None and not offline:
        from .acquisition import _HTTPRepositoryTransport
        fallback = _HTTPRepositoryTransport()
    # Offline: owner LAN / relay bytes only, no Hugging Face source at all.
    # The owner offered a LAN path this Mac cannot use (another network): the
    # Hugging Face origin goes ahead of the owner relay, which stays as the
    # backup.  Every file digest is still checked, whatever the order.  An owner
    # that offered no LAN path (relay-only mode) keeps relay, then Hugging Face.
    return MultiSourceTransport(sources, fallback=fallback, offline=offline,
                                prefer_fallback=offered_lan and not on_owner_lan)


__all__ = [
    "HINT", "LAN_CHUNK_BYTES", "MAX_CHUNK_BYTES", "MultiSourceTransport", "OwnerLanModelSource",
    "MANIFEST_READ", "MANIFEST_RESPONSE", "OwnerModelSource", "PATH_MANIFEST", "PATH_READ", "PeerRangeClient", "RELAY_CHUNK_BYTES", "READ", "RESPONSE",
    "choose_lan_host", "joiner_model_source", "lan_reachable", "owner_snapshot", "peer_source", "validate_hint",
]
