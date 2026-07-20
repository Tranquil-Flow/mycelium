from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import shutil
import ssl
from types import SimpleNamespace

import pytest

from mycelium_demo import cli
from mycelium_demo.device_lab import (
    DeviceLabBundle,
    DeviceLabError,
    prepare_device_lab,
    public_origin_for,
    validate_advertise_host,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.168.10.8", "192.168.10.8"),
        ("10.0.0.4", "10.0.0.4"),
        ("mycelium-lab.local", "mycelium-lab.local"),
    ],
)
def test_validate_advertise_host_accepts_private_device_addresses(
    value: str,
    expected: str,
) -> None:
    assert validate_advertise_host(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "127.0.0.1",
        "::1",
        "fd00::1234",
        "localhost",
        "https://192.168.1.4",
        "host/name",
        "*.example.test",
        "bad host.local",
        "-bad.local",
    ],
)
def test_validate_advertise_host_rejects_loopback_or_malformed_values(value: str) -> None:
    with pytest.raises(DeviceLabError, match="advertise_host_invalid"):
        validate_advertise_host(value)


def test_public_origin_uses_validated_ipv4_or_hostname() -> None:
    assert public_origin_for("192.168.10.8", 443) == "https://192.168.10.8:443"
    assert public_origin_for("mycelium-lab.local", 8787) == "https://mycelium-lab.local:8787"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="OpenSSL not installed")
def test_prepare_device_lab_keeps_ca_stable_rotates_leaf_and_verifies_san(
    tmp_path: Path,
) -> None:
    openssl = shutil.which("openssl")
    assert openssl is not None
    state_root = tmp_path / "device-lab"

    first = prepare_device_lab(
        state_root=state_root,
        advertise_host="192.168.10.8",
        port=8787,
        openssl=openssl,
    )
    first_ca = first.ca_cert.read_bytes()
    first_leaf = first.tls_cert.read_bytes()

    second = prepare_device_lab(
        state_root=state_root,
        advertise_host="192.168.10.8",
        port=8787,
        openssl=openssl,
    )

    assert second.ca_cert.read_bytes() == first_ca
    assert second.tls_cert.read_bytes() != first_leaf
    assert first.public_origin == "https://192.168.10.8:8787"
    assert first.runtime_root == state_root / "runtime"
    assert first.ca_cert.stat().st_mode & 0o777 == 0o644
    assert first.tls_cert.stat().st_mode & 0o777 == 0o644
    assert first.tls_key.stat().st_mode & 0o777 == 0o600
    assert (state_root / "tls" / "ca-key.pem").stat().st_mode & 0o777 == 0o600

    decoded = ssl._ssl._test_decode_cert(str(second.tls_cert))  # type: ignore[attr-defined]
    assert ("IP Address", "192.168.10.8") in decoded["subjectAltName"]
    assert ipaddress.ip_address("192.168.10.8").is_private


@pytest.mark.skipif(not Path("/usr/bin/openssl").exists(), reason="stock macOS OpenSSL unavailable")
def test_prepare_device_lab_supports_stock_macos_libressl(tmp_path: Path) -> None:
    bundle = prepare_device_lab(
        state_root=tmp_path / "stock-openssl-device-lab",
        advertise_host="192.168.10.8",
        port=8787,
        openssl="/usr/bin/openssl",
    )
    assert bundle.ca_cert.is_file()
    assert bundle.tls_cert.is_file()
    assert bundle.tls_key.stat().st_mode & 0o777 == 0o600


def test_prepare_device_lab_rejects_symlink_state_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    state_root = tmp_path / "device-lab"
    state_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(DeviceLabError, match="device_lab_state_invalid"):
        prepare_device_lab(
            state_root=state_root,
            advertise_host="192.168.10.8",
            port=8787,
            openssl="/usr/bin/false",
        )


def test_prepare_device_lab_rejects_group_accessible_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "device-lab"
    state_root.mkdir(mode=0o755)
    state_root.chmod(0o755)

    with pytest.raises(DeviceLabError, match="device_lab_state_permissions_invalid"):
        prepare_device_lab(
            state_root=state_root,
            advertise_host="192.168.10.8",
            port=8787,
            openssl="/usr/bin/false",
        )


def test_device_lab_cli_rejects_unsupported_ipv6_bind() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "device-lab",
                "--advertise-host",
                "192.168.10.8",
                "--bind-host",
                "::",
            ]
        )


def test_device_lab_cli_prepares_then_delegates_to_live_https_without_printing_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []
    prepared: list[dict[str, object]] = []
    state_root = tmp_path / "device-lab"
    bundle = DeviceLabBundle(
        state_root=state_root,
        runtime_root=state_root / "runtime",
        ca_cert=state_root / "tls" / "mycelium-device-lab-ca.crt",
        tls_cert=state_root / "tls" / "server-cert.pem",
        tls_key=state_root / "tls" / "server-key.pem",
        public_origin="https://192.168.10.8:8787",
        advertise_host="192.168.10.8",
        port=8787,
    )

    def fake_prepare(**kwargs):
        prepared.append(dict(kwargs))
        return bundle

    exit_code = cli.main(
        [
            "device-lab",
            "--advertise-host",
            "192.168.10.8",
            "--state-root",
            str(state_root),
        ],
        device_lab_prepare=fake_prepare,
        live_server_main=lambda argv: calls.append(list(argv)) or 0,
        which=lambda command: "/opt/homebrew/bin/openssl" if command == "openssl" else None,
    )

    assert exit_code == 0
    assert prepared == [
        {
            "state_root": state_root,
            "advertise_host": "192.168.10.8",
            "port": 8787,
            "openssl": "/opt/homebrew/bin/openssl",
        }
    ]
    assert calls == [
        [
            "--host",
            "0.0.0.0",
            "--port",
            "8787",
            "--state-root",
            str(state_root / "runtime"),
            "--public-origin",
            "https://192.168.10.8:8787",
            "--tls-cert",
            str(state_root / "tls" / "server-cert.pem"),
            "--tls-key",
            str(state_root / "tls" / "server-key.pem"),
        ]
    ]

    output = capsys.readouterr().out
    record = json.loads(output)
    assert record == {
        "advertise_host": "192.168.10.8",
        "ca_cert": str(state_root / "tls" / "mycelium-device-lab-ca.crt"),
        "device_setup": [
            "transfer_and_trust_ca",
            "open_unique_join_link_per_device",
            "wait_until_each_device_is_ready",
            "run_bounded_test_inference",
        ],
        "local_evidence_only": True,
        "port": 8787,
        "protocol": "mycelium.device_lab_prepared.v1",
        "public_origin": "https://192.168.10.8:8787",
        "route_ready": False,
        "tailscale_required": False,
    }
    assert str(bundle.tls_key) not in output


def test_device_lab_cli_fails_closed_when_openssl_is_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as captured:
        cli.main(
            [
                "device-lab",
                "--advertise-host",
                "192.168.10.8",
                "--state-root",
                str(tmp_path / "device-lab"),
            ],
            which=lambda _command: None,
            live_server_main=lambda _argv: pytest.fail("live server must not start"),
        )

    assert captured.value.code == 2
