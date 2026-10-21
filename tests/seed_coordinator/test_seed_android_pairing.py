# SPDX-License-Identifier: AGPL-3.0-only
"""The owner-run Android pairing channel (``mycelium.pairing_channel.v1``).

The device scans the channel QR, posts its signed pairing request to
``/pair`` and receives the encrypted invitation handoff. The channel is a
bounded, on-demand window of the seed process: it closes after its window or
after its approval budget, refuses replays and malformed requests, and limits
request size and rate.
"""

from __future__ import annotations

import base64
import importlib
import json
from pathlib import Path
import selectors
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_mobile.android_onboarding import (
    decrypt_android_invitation_handoff,
    new_android_pairing_request,
)
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator, SqliteSeedState

QUOTA = {
    "storage_bytes": 1 << 30,
    "network_bytes_per_day": 1 << 30,
    "background_allowed": True,
    "battery_floor_percent": 20,
    "thermal_ceiling_c": 45,
}


def _pairing():
    return importlib.import_module("mycelium_seed.pairing")


def _coordinator(tmp_path: Path, seed_url: str = "https://seed.example.test") -> SeedCoordinator:
    state = tmp_path / "seed-state"
    state.mkdir(mode=0o700)
    database = state / "state.sqlite3"
    return SeedCoordinator(
        swarm_id="swarm-pairing-test",
        seed_node_id="seed-node-pairing",
        seed_url=seed_url,
        signer=generate_ed25519_signer(endpoint_id="seed-pairing-endpoint"),
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-pairing-test",
    )


def _request(device_ref: str = "device-pixel-0001") -> tuple[dict[str, Any], bytes]:
    target = generate_ed25519_signer(endpoint_id="target-android-endpoint")
    return new_android_pairing_request(
        signer=target, device_ref=device_ref, now=time.time(), ttl_seconds=300
    )


def _post(
    url: str,
    body: bytes,
    *,
    content_type: str = "application/json",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, **(headers or {})},
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _channel(tmp_path: Path, **overrides: Any):
    options: dict[str, Any] = {
        "quota": QUOTA,
        "host": "127.0.0.1",
        "port": 0,
        "pairing_url": "https://pair.example.test/pair",
        "trusted_https_proxy": True,
        "window_seconds": 60,
        "max_approvals": 1,
    }
    options.update(overrides)
    channel = _pairing().AndroidPairingChannel(_coordinator(tmp_path), **options)
    channel.start()
    return channel


def test_valid_request_gets_an_encrypted_handoff_bound_to_the_https_seed(
    tmp_path: Path,
) -> None:
    channel = _channel(tmp_path)
    try:
        qr = channel.channel_qr
        assert qr.startswith("mycelium:")
        encoded = qr[len("mycelium:") :]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        document = json.loads(raw)
        assert canonical_json_bytes(document) == raw
        assert document["protocol"] == "mycelium.pairing_channel.v1"
        assert document["pairing_url"] == "https://pair.example.test/pair"
        assert isinstance(document["expires_at"], int)

        request, private_key = _request()
        status, body = _post(channel.local_url, canonical_json_bytes({"request": request}))
        assert status == 200, body
        payload = decrypt_android_invitation_handoff(
            request=request,
            handoff=body["handoff"],
            recipient_private_key=private_key,
            now=time.time(),
        )
        invite = verify_invite_bundle(payload["invite_bundle"], now=time.time())
        assert invite["payload"]["seed_url"] == "https://seed.example.test"
        assert invite["payload"]["nonce"] == request["pairing_nonce"]
        assert payload["quota"] == QUOTA
    finally:
        channel.close()


def test_channel_closes_after_its_approval_budget(tmp_path: Path) -> None:
    channel = _channel(tmp_path, max_approvals=1)
    try:
        first, _ = _request("device-pixel-0001")
        assert _post(channel.local_url, canonical_json_bytes({"request": first}))[0] == 200
        assert channel.wait_closed(timeout=5)
        assert channel.close_reason == "approvals_exhausted"
        second, _ = _request("device-pixel-0002")
        with pytest.raises((URLError, ConnectionError, OSError)):
            urlopen(
                Request(
                    channel.local_url,
                    data=canonical_json_bytes({"request": second}),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                ),
                timeout=2,
            )
    finally:
        channel.close()


def test_channel_closes_when_its_window_expires(tmp_path: Path) -> None:
    clock = {"now": time.time()}
    channel = _channel(
        tmp_path, window_seconds=30, max_approvals=4, clock=lambda: clock["now"]
    )
    try:
        clock["now"] += 31
        assert channel.wait_closed(timeout=5)
        assert channel.close_reason == "window_expired"
    finally:
        channel.close()


def test_replayed_request_is_refused(tmp_path: Path) -> None:
    channel = _channel(tmp_path, max_approvals=4)
    try:
        request, _ = _request()
        body = canonical_json_bytes({"request": request})
        assert _post(channel.local_url, body)[0] == 200
        status, refused = _post(channel.local_url, body)
        assert status == 409
        assert refused == {"error": {"code": "pairing_nonce_reused"}}
        assert channel.approvals == 1
    finally:
        channel.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: {**request, "device_ref": "device-tampered-0001"},
        lambda request: {**request, "expires_at": request["issued_at"] - 1},
        lambda request: {"protocol": "mycelium.pairing_request.v1"},
    ],
)
def test_invalid_or_unsigned_requests_are_refused_without_a_handoff(
    tmp_path: Path, mutate: Any
) -> None:
    channel = _channel(tmp_path, max_approvals=4)
    try:
        request, _ = _request()
        status, body = _post(channel.local_url, canonical_json_bytes({"request": mutate(request)}))
        assert status == 400
        assert set(body) == {"error"}
        assert "handoff" not in json.dumps(body)
        assert channel.approvals == 0
    finally:
        channel.close()


def test_request_shape_size_type_route_and_method_are_bounded(tmp_path: Path) -> None:
    channel = _channel(tmp_path, max_approvals=4, max_body_bytes=4096)
    try:
        request, _ = _request()
        assert _post(channel.local_url, canonical_json_bytes({"request": request, "extra": 1}))[0] == 400
        assert _post(channel.local_url, b"{" + b" " * 5000 + b"}")[0] == 413
        assert _post(channel.local_url, b"not json")[0] == 400
        assert (
            _post(
                channel.local_url,
                canonical_json_bytes({"request": request}),
                content_type="text/plain",
            )[0]
            == 415
        )
        other = channel.local_url.rsplit("/", 1)[0] + "/other"
        assert _post(other, canonical_json_bytes({"request": request}))[0] == 404
        with pytest.raises(HTTPError) as caught:
            urlopen(channel.local_url, timeout=2)
        assert caught.value.code == 405
        assert channel.approvals == 0
    finally:
        channel.close()


def test_forwarded_cleartext_is_refused_behind_the_trusted_proxy(tmp_path: Path) -> None:
    channel = _channel(tmp_path, max_approvals=4)
    try:
        request, _ = _request()
        status, body = _post(
            channel.local_url,
            canonical_json_bytes({"request": request}),
            headers={"X-Forwarded-Proto": "http"},
        )
        assert status == 400
        assert body == {"error": {"code": "pairing_downgrade_refused"}}
    finally:
        channel.close()


def test_request_rate_is_limited(tmp_path: Path) -> None:
    channel = _channel(
        tmp_path, max_approvals=4, max_requests_per_second=1, rate_capacity=2
    )
    try:
        statuses = [_post(channel.local_url, b"{}")[0] for _ in range(6)]
        assert 429 in statuses
    finally:
        channel.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"trusted_https_proxy": False, "pairing_url": "https://pair.example.test/pair"},
        {"trusted_https_proxy": True, "pairing_url": "http://127.0.0.1:18866/pair"},
        {"trusted_https_proxy": True, "pairing_url": None},
        {"trusted_https_proxy": True, "pairing_url": "https://pair.example.test/other"},
        {"trusted_https_proxy": True, "host": "0.0.0.0"},
        {"window_seconds": 0},
        {"window_seconds": 3601},
        {"max_approvals": 0},
        {"max_approvals": 17},
        {"quota": {**QUOTA, "battery_floor_percent": 101}},
        {"quota": {}},
    ],
)
def test_construction_refuses_unsafe_configuration(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    options: dict[str, Any] = {
        "quota": QUOTA,
        "host": "127.0.0.1",
        "port": 0,
        "pairing_url": "https://pair.example.test/pair",
        "trusted_https_proxy": True,
        "window_seconds": 60,
        "max_approvals": 1,
    }
    options.update(overrides)
    with pytest.raises(ValueError):
        _pairing().AndroidPairingChannel(_coordinator(tmp_path), **options)


def test_plain_http_channel_defaults_to_its_local_pairing_url(tmp_path: Path) -> None:
    channel = _channel(tmp_path, trusted_https_proxy=False, pairing_url=None)
    try:
        assert channel.pairing_url == channel.local_url
        assert channel.local_url.startswith("http://127.0.0.1:")
        assert channel.local_url.endswith("/pair")
    finally:
        channel.close()


def _read_line(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        assert selector.select(timeout=10), f"no status; returncode={process.poll()}"
        line = process.stdout.readline()
    finally:
        selector.close()
    assert line
    return json.loads(line)


def test_seed_process_opens_a_bounded_pairing_window_on_demand(tmp_path: Path) -> None:
    data_dir = tmp_path / "seed"
    quota_file = tmp_path / "quota.json"
    quota_file.write_bytes(canonical_json_bytes(QUOTA))
    base = [sys.executable, "-m", "mycelium_seed", "--bind", "127.0.0.1", "--port", "0", "--data-dir", str(data_dir)]
    initialized = subprocess.run(
        [*base, "--init-only"], capture_output=True, text=True, timeout=10, check=False
    )
    assert initialized.returncode == 0, initialized.stderr
    process = subprocess.Popen(
        [
            *base,
            "--advertised-url",
            "https://seed.example.test",
            "--trusted-https-proxy",
            "--pairing-port",
            "0",
            "--pairing-url",
            "https://pair.example.test/pair",
            "--pairing-quota-file",
            str(quota_file),
            "--pairing-window-seconds",
            "120",
            "--pairing-max-approvals",
            "1",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        started = _read_line(process)
        assert started["event"] == "seed_started"
        opened = _read_line(process)
        assert opened["event"] == "pairing_channel_open"
        assert opened["pairing_url"] == "https://pair.example.test/pair"
        assert opened["max_approvals"] == 1
        assert opened["invite_qr"].startswith("mycelium:")

        request, private_key = _request()
        status, body = _post(opened["local_url"], canonical_json_bytes({"request": request}))
        assert status == 200, body
        payload = decrypt_android_invitation_handoff(
            request=request,
            handoff=body["handoff"],
            recipient_private_key=private_key,
            now=time.time(),
        )
        invite = verify_invite_bundle(payload["invite_bundle"], now=time.time())
        assert invite["payload"]["seed_url"] == "https://seed.example.test"

        approved = _read_line(process)
        assert approved["event"] == "pairing_approved"
        assert approved["device_ref"] == "device-pixel-0001"
        closed = _read_line(process)
        assert closed == {
            "event": "pairing_channel_closed",
            "protocol": "mycelium.seed_main_status.v1",
            "reason": "approvals_exhausted",
            "approvals": 1,
        }
        assert process.poll() is None
    finally:
        process.terminate()
        process.communicate(timeout=5)
    assert process.returncode == 0


@pytest.mark.parametrize(
    ("name", "extra"),
    [
        ("missing-quota", ["--pairing-port", "18866"]),
        ("quota-without-port", ["--pairing-quota-file", "QUOTA"]),
        ("https-pair-url-without-proxy", ["--pairing-port", "18866", "--pairing-quota-file", "QUOTA", "--pairing-url", "https://pair.example.test/pair"]),
        ("same-port", ["--pairing-port", "18865", "--pairing-quota-file", "QUOTA"]),
        ("url-without-port", ["--pairing-url", "http://127.0.0.1:18866/pair"]),
        ("bad-approvals", ["--pairing-port", "18866", "--pairing-quota-file", "QUOTA", "--pairing-max-approvals", "17"]),
        ("bad-window", ["--pairing-port", "18866", "--pairing-quota-file", "QUOTA", "--pairing-window-seconds", "0"]),
        ("bad-quota", ["--pairing-port", "18866", "--pairing-quota-file", "BADQUOTA"]),
    ],
)
def test_seed_pairing_flags_are_validated_in_preflight(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    extra: list[str],
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    quota = tmp_path / "quota.json"
    quota.write_bytes(canonical_json_bytes(QUOTA))
    bad = tmp_path / "bad.json"
    bad.write_bytes(canonical_json_bytes({**QUOTA, "storage_bytes": -1}))
    extra = [str(quota) if item == "QUOTA" else str(bad) if item == "BADQUOTA" else item for item in extra]
    data_dir = tmp_path / name
    data_dir.mkdir(mode=0o700)
    with pytest.raises(seed_main._EntrypointFailure) as caught:
        seed_main.run(
            ["--bind", "127.0.0.1", "--port", "18865", "--data-dir", str(data_dir), "--dry-run", *extra]
        )
    assert caught.value.code == "seed_preflight_failed"
    capsys.readouterr()


def test_seed_pairing_flags_accept_a_valid_plain_http_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    quota = tmp_path / "quota.json"
    quota.write_bytes(canonical_json_bytes(QUOTA))
    data_dir = tmp_path / "ok"
    data_dir.mkdir(mode=0o700)
    assert (
        seed_main.run(
            [
                "--bind",
                "127.0.0.1",
                "--port",
                "18865",
                "--data-dir",
                str(data_dir),
                "--dry-run",
                "--pairing-port",
                "18866",
                "--pairing-quota-file",
                str(quota),
            ]
        )
        == 0
    )
    capsys.readouterr()
