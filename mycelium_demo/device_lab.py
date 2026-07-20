from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import stat
import subprocess
from typing import Any


class DeviceLabError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceLabBundle:
    state_root: Path
    runtime_root: Path
    ca_cert: Path
    tls_cert: Path
    tls_key: Path
    public_origin: str
    advertise_host: str
    port: int


_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def validate_advertise_host(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 253
        or not value.isascii()
        or any(character.isspace() or ord(character) < 0x21 for character in value)
    ):
        raise DeviceLabError("advertise_host_invalid")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if all(character in "0123456789." for character in value):
            raise DeviceLabError("advertise_host_invalid") from None
        labels = value.rstrip(".").split(".")
        if (
            value.endswith(".")
            or value.casefold() == "localhost"
            or len(labels) < 2
            or any(_DNS_LABEL.fullmatch(label) is None for label in labels)
        ):
            raise DeviceLabError("advertise_host_invalid") from None
        return value.casefold()
    if address.version != 4 or address.is_loopback or address.is_unspecified or address.is_multicast:
        raise DeviceLabError("advertise_host_invalid")
    return address.compressed


def public_origin_for(advertise_host: str, port: int) -> str:
    host = validate_advertise_host(advertise_host)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise DeviceLabError("device_lab_port_invalid")
    display_host = f"[{host}]" if ":" in host else host
    return f"https://{display_host}:{port}"


def _ensure_private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise DeviceLabError("device_lab_state_invalid")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise DeviceLabError("device_lab_state_permissions_invalid")
        return
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)


def _validate_regular_file(path: Path, *, private: bool) -> None:
    if path.is_symlink() or not path.is_file():
        raise DeviceLabError("device_lab_tls_state_invalid")
    if private and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise DeviceLabError("device_lab_tls_permissions_invalid")


def _run_openssl(
    openssl: str,
    arguments: list[str],
    *,
    runner: Callable[..., Any],
    operation: str,
) -> None:
    try:
        completed = runner(
            [openssl, *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise DeviceLabError(f"openssl_failed:{operation}") from exc
    if int(completed.returncode) != 0:
        raise DeviceLabError(f"openssl_failed:{operation}")


def _verify_certificate_san(certificate: Path, advertise_host: str) -> None:
    try:
        decoded = ssl._ssl._test_decode_cert(str(certificate))  # type: ignore[attr-defined]
        subject_alt_names = decoded.get("subjectAltName", ())
    except (OSError, ssl.SSLError, TypeError, ValueError) as exc:
        raise DeviceLabError("openssl_failed:verify_server_san") from exc
    try:
        address = ipaddress.ip_address(advertise_host)
    except ValueError:
        matched = any(
            kind == "DNS" and str(value).rstrip(".").lower() == advertise_host.rstrip(".").lower()
            for kind, value in subject_alt_names
        )
    else:
        matched = False
        for kind, value in subject_alt_names:
            if kind != "IP Address":
                continue
            try:
                matched = ipaddress.ip_address(str(value)) == address
            except ValueError:
                continue
            if matched:
                break
    if not matched:
        raise DeviceLabError("openssl_failed:verify_server_san")


def _generate_ca(
    *,
    openssl: str,
    tls_root: Path,
    ca_cert: Path,
    ca_key: Path,
    runner: Callable[..., Any],
) -> None:
    temporary = tls_root / f".ca-{secrets.token_hex(8)}"
    temporary.mkdir(mode=0o700)
    temporary_key = temporary / "ca-key.pem"
    temporary_cert = temporary / "ca-cert.pem"
    try:
        _run_openssl(
            openssl,
            [
                "genpkey",
                "-algorithm",
                "EC",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-out",
                str(temporary_key),
            ],
            runner=runner,
            operation="generate_ca_key",
        )
        temporary_key.chmod(0o600)
        _run_openssl(
            openssl,
            [
                "req",
                "-x509",
                "-new",
                "-sha256",
                "-key",
                str(temporary_key),
                "-out",
                str(temporary_cert),
                "-days",
                "3650",
                "-subj",
                "/CN=Mycelium Device Lab Local CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-addext",
                "subjectKeyIdentifier=hash",
            ],
            runner=runner,
            operation="generate_ca_certificate",
        )
        temporary_cert.chmod(0o644)
        os.replace(temporary_key, ca_key)
        os.replace(temporary_cert, ca_cert)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _ensure_ca(
    *,
    openssl: str,
    tls_root: Path,
    ca_cert: Path,
    ca_key: Path,
    runner: Callable[..., Any],
) -> None:
    cert_exists = ca_cert.exists() or ca_cert.is_symlink()
    key_exists = ca_key.exists() or ca_key.is_symlink()
    if cert_exists != key_exists:
        raise DeviceLabError("device_lab_ca_partial")
    if not cert_exists:
        _generate_ca(
            openssl=openssl,
            tls_root=tls_root,
            ca_cert=ca_cert,
            ca_key=ca_key,
            runner=runner,
        )
    _validate_regular_file(ca_cert, private=False)
    _validate_regular_file(ca_key, private=True)
    _run_openssl(
        openssl,
        ["x509", "-in", str(ca_cert), "-noout", "-checkend", "86400"],
        runner=runner,
        operation="verify_ca_certificate",
    )


def _generate_leaf(
    *,
    openssl: str,
    tls_root: Path,
    ca_cert: Path,
    ca_key: Path,
    tls_cert: Path,
    tls_key: Path,
    advertise_host: str,
    runner: Callable[..., Any],
) -> None:
    temporary = tls_root / f".leaf-{secrets.token_hex(8)}"
    temporary.mkdir(mode=0o700)
    temporary_key = temporary / "server-key.pem"
    request = temporary / "server.csr"
    extension = temporary / "server-ext.cnf"
    temporary_cert = temporary / "server-cert.pem"
    try:
        try:
            address = ipaddress.ip_address(advertise_host)
        except ValueError:
            alt_name = f"DNS.1 = {advertise_host}"
        else:
            alt_name = f"IP.1 = {address.compressed}"
        extension.write_text(
            "[server_cert]\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=@alt_names\n"
            "[alt_names]\n"
            f"{alt_name}\n",
            encoding="ascii",
        )
        extension.chmod(0o600)
        _run_openssl(
            openssl,
            [
                "genpkey",
                "-algorithm",
                "EC",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-out",
                str(temporary_key),
            ],
            runner=runner,
            operation="generate_server_key",
        )
        temporary_key.chmod(0o600)
        _run_openssl(
            openssl,
            [
                "req",
                "-new",
                "-sha256",
                "-key",
                str(temporary_key),
                "-out",
                str(request),
                "-subj",
                f"/CN={advertise_host}",
            ],
            runner=runner,
            operation="generate_server_request",
        )
        _run_openssl(
            openssl,
            [
                "x509",
                "-req",
                "-in",
                str(request),
                "-CA",
                str(ca_cert),
                "-CAkey",
                str(ca_key),
                "-set_serial",
                f"0x{secrets.token_hex(16)}",
                "-out",
                str(temporary_cert),
                "-days",
                "30",
                "-sha256",
                "-extfile",
                str(extension),
                "-extensions",
                "server_cert",
            ],
            runner=runner,
            operation="sign_server_certificate",
        )
        temporary_cert.chmod(0o644)
        _run_openssl(
            openssl,
            ["x509", "-in", str(temporary_cert), "-noout", "-checkend", "86400"],
            runner=runner,
            operation="verify_server_certificate",
        )
        _verify_certificate_san(temporary_cert, advertise_host)
        _run_openssl(
            openssl,
            ["verify", "-CAfile", str(ca_cert), str(temporary_cert)],
            runner=runner,
            operation="verify_server_chain",
        )
        os.replace(temporary_key, tls_key)
        os.replace(temporary_cert, tls_cert)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def prepare_device_lab(
    *,
    state_root: Path,
    advertise_host: str,
    port: int,
    openssl: str,
    runner: Callable[..., Any] = subprocess.run,
) -> DeviceLabBundle:
    host = validate_advertise_host(advertise_host)
    origin = public_origin_for(host, port)
    root = Path(os.path.abspath(os.fspath(state_root.expanduser())))
    _ensure_private_directory(root)
    tls_root = root / "tls"
    _ensure_private_directory(tls_root)
    ca_cert = tls_root / "mycelium-device-lab-ca.crt"
    ca_key = tls_root / "ca-key.pem"
    tls_cert = tls_root / "server-cert.pem"
    tls_key = tls_root / "server-key.pem"
    _ensure_ca(
        openssl=openssl,
        tls_root=tls_root,
        ca_cert=ca_cert,
        ca_key=ca_key,
        runner=runner,
    )
    _generate_leaf(
        openssl=openssl,
        tls_root=tls_root,
        ca_cert=ca_cert,
        ca_key=ca_key,
        tls_cert=tls_cert,
        tls_key=tls_key,
        advertise_host=host,
        runner=runner,
    )
    _validate_regular_file(tls_cert, private=False)
    _validate_regular_file(tls_key, private=True)
    return DeviceLabBundle(
        state_root=root,
        runtime_root=root / "runtime",
        ca_cert=ca_cert,
        tls_cert=tls_cert,
        tls_key=tls_key,
        public_origin=origin,
        advertise_host=host,
        port=port,
    )
