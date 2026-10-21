# SPDX-License-Identifier: AGPL-3.0-only
"""Actual loopback TLS around the canonical seed HTTP transport."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import stat
from typing import Any, Mapping
from urllib.request import BaseHandler, HTTPSHandler, HTTPRedirectHandler, build_opener
from urllib.parse import urlsplit
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPServer


class LocalTLSError(RuntimeError):
    """Stable local TLS setup error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LocalTLSMaterial:
    ca_certificate: Path
    server_certificate: Path
    server_private_key: Path


class _OwnerLoopbackTransport(BaseHandler):
    """Retain public identity checks while dialing this owner's verified TLS."""
    handler_order=100
    def __init__(self, public_origin, transport_url):
        parts=urlsplit(transport_url)
        if (parts.scheme!='https' or parts.hostname!='127.0.0.1'
                or parts.username is not None or parts.password is not None
                or parts.path not in ('','/') or parts.query or parts.fragment
                or parts.port is None or not 49152<=parts.port<=65535):
            raise LocalTLSError('local_tls_transport_invalid')
        self.public_origin=public_origin.rstrip('/')
        self.transport_url=transport_url.rstrip('/')
    def https_request(self,request):
        prefix=self.public_origin+'/'
        if not request.full_url.startswith(prefix):
            raise LocalTLSError('local_tls_transport_invalid')
        request.full_url=self.transport_url+request.full_url[len(self.public_origin):]
        return request


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _private_directory(path: Path, *, create: bool) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    import mycelium_host_platform

    if mycelium_host_platform.is_win32():
        # Owner-only protected DACL (inherited by the key material below).
        try:
            with mycelium_host_platform.private_directory(absolute, create=create) as lease:
                if not lease.exists:
                    raise LocalTLSError("local_tls_path_invalid")
        except mycelium_host_platform.HostPlatformError as exc:
            raise LocalTLSError("local_tls_path_invalid") from exc
        return absolute
    try:
        if create and not absolute.exists() and not absolute.is_symlink():
            absolute.mkdir(parents=True, mode=0o700)
            absolute.chmod(0o700)
        metadata = absolute.lstat()
    except OSError as exc:
        raise LocalTLSError("local_tls_path_invalid") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise LocalTLSError("local_tls_path_invalid")
    return absolute


def _owner_file(path: Path) -> None:
    import mycelium_host_platform

    if mycelium_host_platform.is_win32():
        try:
            with mycelium_host_platform.private_directory(path.parent, create=False) as lease:
                lease.read_file(path.name, max_bytes=1 << 20)  # owner-only, single link, no reparse
        except mycelium_host_platform.HostPlatformError as exc:
            code = "local_tls_material_incomplete" if exc.code == "host_file_missing" else "local_tls_material_invalid"
            raise LocalTLSError(code) from exc
        return
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LocalTLSError("local_tls_material_incomplete") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise LocalTLSError("local_tls_material_invalid")


def _write_temporary(path: Path, body: bytes) -> Path:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if hasattr(os, "fchmod") and os.name != "nt":
            os.fchmod(descriptor, 0o600)  # win32: the directory's owner-only DACL is inherited
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short TLS material write")
            view = view[written:]
        os.fsync(descriptor)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise LocalTLSError("local_tls_material_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return temporary


def _generate_material() -> tuple[bytes, bytes, bytes]:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Mycelium local installation CA")]
    )
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
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
    server_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Mycelium loopback seed")]
    )
    server = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
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
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
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
        .sign(ca_key, hashes.SHA256())
    )
    return (
        ca.public_bytes(serialization.Encoding.PEM),
        server.public_bytes(serialization.Encoding.PEM),
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _validate_material(material: LocalTLSMaterial) -> None:
    for path in (
        material.ca_certificate,
        material.server_certificate,
        material.server_private_key,
    ):
        _owner_file(path)
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            str(material.server_certificate), str(material.server_private_key)
        )
        client = ssl.create_default_context(cafile=str(material.ca_certificate))
        if not client.check_hostname or client.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("client verification disabled")
        certificate = x509.load_pem_x509_certificate(
            material.server_certificate.read_bytes()
        )
        ca = x509.load_pem_x509_certificate(material.ca_certificate.read_bytes())
        if certificate.issuer != ca.subject:
            raise ValueError("issuer mismatch")
    except (OSError, ValueError, ssl.SSLError) as exc:
        raise LocalTLSError("local_tls_material_invalid") from exc


def _install_material_win32(directory: Path, paths, bodies) -> None:
    """Install each file with an explicit owner-only protected DACL (no inheritance trust)."""

    import mycelium_host_platform

    installed: list[str] = []
    try:
        with mycelium_host_platform.private_directory(directory, create=False) as lease:
            for path, body in zip(paths, bodies, strict=True):
                try:
                    lease.create_file_exclusive(path.name, body)
                except mycelium_host_platform.HostPlatformError as exc:
                    if exc.code == "host_file_exists":
                        raise LocalTLSError("local_tls_material_incomplete") from None
                    raise
                installed.append(path.name)
    except BaseException as exc:
        for name in installed:
            (directory / name).unlink(missing_ok=True)
        if isinstance(exc, LocalTLSError):
            raise
        if isinstance(exc, mycelium_host_platform.HostPlatformError):
            raise LocalTLSError("local_tls_material_write_failed") from exc
        raise


def ensure_local_tls_material(root: Path) -> LocalTLSMaterial:
    """Load one complete local TLS identity or atomically create its first generation."""

    directory = _private_directory(root, create=True)
    material = LocalTLSMaterial(
        directory / "ca.pem",
        directory / "server.pem",
        directory / "server.key",
    )
    paths = (
        material.ca_certificate,
        material.server_certificate,
        material.server_private_key,
    )
    present = [path.exists() or path.is_symlink() for path in paths]
    if any(present):
        if not all(present):
            raise LocalTLSError("local_tls_material_incomplete")
        _validate_material(material)
        return material

    bodies = _generate_material()
    import mycelium_host_platform

    if mycelium_host_platform.is_win32():
        _install_material_win32(directory, paths, bodies)
        _validate_material(material)
        return material
    temporaries: list[Path] = []
    installed: list[Path] = []
    try:
        temporaries = [
            _write_temporary(path, body) for path, body in zip(paths, bodies, strict=True)
        ]
        if any(path.exists() or path.is_symlink() for path in paths):
            raise LocalTLSError("local_tls_material_incomplete")
        for temporary, path in zip(temporaries, paths, strict=True):
            os.replace(temporary, path)
            installed.append(path)
        if os.name != "nt":  # win32: MoveFileEx metadata is journaled; no directory handle fsync
            descriptor = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except LocalTLSError:
        for path in installed:
            path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        for path in installed:
            path.unlink(missing_ok=True)
        raise LocalTLSError("local_tls_material_write_failed") from exc
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)
    _validate_material(material)
    return material


def _dynamic_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


class LocalTLSSeedServer:
    """TLS socket wrapper preserving the canonical SeedHTTPServer handler."""

    def __init__(
        self,
        coordinator: SeedCoordinator,
        *,
        material: LocalTLSMaterial,
        control_mailbox=None,
        public_origin: str | None = None,
        port: int | None = None,
    ) -> None:
        if not isinstance(coordinator, SeedCoordinator):
            raise ValueError("coordinator is invalid")
        _validate_material(material)
        last_error: OSError | None = None
        canonical: SeedHTTPServer | None = None
        if port is not None and (type(port) is not int or not 49152<=port<=65535):
            raise LocalTLSError('local_tls_port_invalid')
        selected_port=port
        for _attempt in range(1 if selected_port is not None else 10):
            port = _dynamic_loopback_port() if selected_port is None else selected_port
            origin = f"https://127.0.0.1:{port}"
            try:
                canonical = SeedHTTPServer(
                    coordinator,
                    host="127.0.0.1",
                    port=port,
                    public_seed_url=origin if public_origin is None else public_origin,
                )
            except OSError as exc:
                last_error = exc
                continue
            self.base_url = origin
            break
        if canonical is None:
            raise LocalTLSError("local_tls_bind_failed") from last_error
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(
                str(material.server_certificate), str(material.server_private_key)
            )
            canonical._server.socket = context.wrap_socket(  # noqa: SLF001
                canonical._server.socket,  # noqa: SLF001
                server_side=True,
            )
        except Exception:
            canonical.close()
            raise
        if control_mailbox is not None:
            from mycelium_seed.http import _SeedRequestHandler
            from .managed_control import ManagedControlMailbox, _ManagedControlRequestHandler
            from threading import BoundedSemaphore
            if not isinstance(control_mailbox, ManagedControlMailbox):
                canonical.close()
                raise ValueError('control mailbox is invalid')
            slots=BoundedSemaphore(16)
            class CombinedSeedHandler(_SeedRequestHandler):
                def do_POST(self):
                    if not self.path.startswith('/managed-control/'):
                        return super().do_POST()
                    if not slots.acquire(blocking=False):
                        return _ManagedControlRequestHandler._send(self,503,{'error':{'code':'managed_control_capacity_exhausted'}})
                    try:
                        if (self.headers.get('Transfer-Encoding') is not None
                            or len(self.headers.get_all('Content-Length',[]))!=1
                            or self.headers.get('Cookie') is not None
                            or self.headers.get('Authorization') is not None):
                            return _ManagedControlRequestHandler._send(self,400,{'error':{'code':'managed_control_headers_invalid'}})
                        return _ManagedControlRequestHandler.do_POST(self)
                    finally:slots.release()
                def _send(self,status,document):
                    if self.path.startswith('/managed-control/'):
                        return _ManagedControlRequestHandler._send(self,status,document)
                    return super()._send(status,document)
            canonical._server.mailbox=control_mailbox
            canonical._server.RequestHandlerClass=CombinedSeedHandler
        self.canonical_server = canonical
        self.material = material

    def start(self) -> "LocalTLSSeedServer":
        self.canonical_server.start()
        return self

    def close(self) -> None:
        self.canonical_server.close()

    def __enter__(self) -> "LocalTLSSeedServer":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


def pinned_seed_client_from_invite(
    bundle: Mapping[str, Any],
    *,
    now: float,
    ca_certificate: Path,
    timeout: float = 10.0,
    transport_url: str | None = None,
) -> SeedHTTPClient:
    """Return the canonical seed client with one installation-local CA pin."""

    _owner_file(ca_certificate)
    client = SeedHTTPClient.from_invite_bundle(bundle, now=now, timeout=timeout)
    try:
        context = ssl.create_default_context(cafile=str(ca_certificate))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    except (OSError, ssl.SSLError) as exc:
        raise LocalTLSError("local_tls_material_invalid") from exc
    client._opener = build_opener(  # noqa: SLF001 - explicit canonical-client adapter
        _NoRedirect(),
        HTTPSHandler(context=context),
        *(() if transport_url is None else (_OwnerLoopbackTransport(client.seed_url,transport_url),)),
    )
    return client


def pinned_seed_client(
    *,
    seed_url: str,
    swarm_id: str,
    seed_key_digest: str,
    seed_key_records: list[Mapping[str, Any]],
    ca_certificate: Path,
    timeout: float = 10.0,
    transport_url: str | None = None,
) -> SeedHTTPClient:
    """Return a canonical client for durable resume without an invite bundle."""

    _owner_file(ca_certificate)
    client = SeedHTTPClient(
        seed_url=seed_url,
        swarm_id=swarm_id,
        seed_key_digest=seed_key_digest,
        seed_key_records=seed_key_records,
        timeout=timeout,
    )
    context = ssl.create_default_context(cafile=str(ca_certificate))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    client._opener = build_opener(  # noqa: SLF001
        _NoRedirect(),
        HTTPSHandler(context=context),
        *(() if transport_url is None else (_OwnerLoopbackTransport(client.seed_url,transport_url),)),
    )
    return client


__all__ = [
    "LocalTLSError",
    "LocalTLSMaterial",
    "LocalTLSSeedServer",
    "ensure_local_tls_material",
    "pinned_seed_client",
    "pinned_seed_client_from_invite",
]
