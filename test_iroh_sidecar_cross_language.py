# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
from typing import Any

import pytest

from mycelium_iroh_sidecar import (
    AuthenticationError,
    OPERATIONAL_MAX_FRAME_BYTES,
    SidecarClient,
    SidecarError,
)
from mycelium_iroh_sidecar import client as sidecar_client_module

ROOT = Path(__file__).resolve().parent
CRATE = ROOT / "native" / "iroh_transport"
BINARY = CRATE / "target" / "debug" / "mycelium-iroh-sidecar"
GOLDEN_DIR = ROOT / "contracts" / "router-wire-golden"
GOLDEN = GOLDEN_DIR / "01-hop-header.bin"


@pytest.fixture(scope="session", autouse=True)
def _build_sidecar() -> None:
    subprocess.run(
        ["cargo", "build", "--locked", "--bin", "mycelium-iroh-sidecar"],
        cwd=CRATE,
        check=True,
    )


class RunningSidecar:
    def __init__(self, base: Path, secret: bytes, *, queue_capacity: int = 8):
        self.base = base
        self.secret = secret
        self.socket_path = base / "run" / "sidecar.sock"
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, secret)
        finally:
            os.close(write_fd)
        self.process = subprocess.Popen(
            [
                str(BINARY),
                "--uds",
                str(self.socket_path),
                "--bootstrap-fd",
                str(read_fd),
                "--local-only",
                "--queue-capacity",
                str(queue_capacity),
            ],
            cwd=ROOT,
            pass_fds=(read_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(read_fd)
        self.ready = self._read_ready()

    def _read_ready(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        readable, _, _ = select.select([self.process.stdout], [], [], 20)
        if not readable:
            self.stop()
            raise AssertionError("sidecar did not become ready")
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise AssertionError(f"sidecar exited before ready: {stderr}")
        ready = json.loads(line)
        assert ready["event"] == "ready"
        assert ready["alpn"] == "mycelium.iroh.sidecar.v1"
        return ready

    def client(self, secret: bytes | None = None) -> SidecarClient:
        client = SidecarClient(self.socket_path, secret if secret is not None else self.secret)
        client.connect()
        return client

    def stop(self) -> str:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        return self.process.stderr.read() if self.process.stderr else ""


@pytest.fixture
def short_root():
    root = Path(tempfile.mkdtemp(prefix="mycelium-p7-", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def sidecars(short_root: Path):
    first = RunningSidecar(short_root / "first", bytes(range(32)))
    second = RunningSidecar(short_root / "second", bytes(range(32, 64)))
    try:
        yield first, second
    finally:
        first.stop()
        second.stop()


def configure_pair(first: RunningSidecar, second: RunningSidecar):
    first_client = first.client()
    second_client = second.client()
    first_client.configure_peer(second.ready["endpoint_id"], second.ready["endpoint_addr"])
    second_client.configure_peer(first.ready["endpoint_id"], first.ready["endpoint_addr"])
    return first_client, second_client


def test_socket_permissions_peer_auth_and_hmac_rejection(sidecars) -> None:
    first, _ = sidecars
    assert stat.S_IMODE(first.socket_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(first.socket_path.stat().st_mode) == 0o600

    wrong = SidecarClient(first.socket_path, b"x" * 32)
    with pytest.raises(AuthenticationError):
        wrong.connect()

    good = first.client()
    try:
        good.ping()
    finally:
        good.close()


def test_endpoint_key_pin_must_match_endpoint_address(sidecars) -> None:
    first, second = sidecars
    client = first.client()
    try:
        with pytest.raises(SidecarError, match="invalid_peer"):
            client.configure_peer(
                first.ready["endpoint_id"], second.ready["endpoint_addr"]
            )
    finally:
        client.close()


def test_unauthenticated_idle_sessions_cannot_starve_local_socket(
    sidecars,
) -> None:
    first, _ = sidecars
    idle_connections: list[socket.socket] = []
    try:
        for _ in range(16):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(os.fspath(first.socket_path))
            idle_connections.append(connection)
        time.sleep(2.25)
        client = SidecarClient(first.socket_path, first.secret, timeout=3.0)
        client.connect()
        try:
            client.ping()
        finally:
            client.close()
    finally:
        for connection in idle_connections:
            connection.close()


def test_two_rust_sidecars_transfer_only_canonical_router_frames(sidecars) -> None:
    first, second = sidecars
    sender, receiver = configure_pair(first, second)
    frame_paths = sorted(GOLDEN_DIR.glob("*.bin"))
    assert len(frame_paths) == 10
    try:
        for frame_path in frame_paths:
            frame = frame_path.read_bytes()
            message_id = sender.send(frame)
            delivered_id, delivered = receiver.recv(timeout=10)
            assert delivered_id == message_id
            assert delivered == frame
            receiver.ack(delivered_id)

        with pytest.raises(ValueError, match="canonical Router ingress"):
            sender.send(b"not-router-wire")
        with pytest.raises(ValueError, match="16 MiB"):
            sender.send(b"x" * (OPERATIONAL_MAX_FRAME_BYTES + 1))
    finally:
        sender.close()
        receiver.close()


def test_remote_rejection_reconnects_and_retries_pending_delivery(sidecars) -> None:
    first, second = sidecars
    sender = first.client()
    receiver = second.client()
    frame = GOLDEN.read_bytes()
    try:
        sender.configure_peer(
            second.ready["endpoint_id"], second.ready["endpoint_addr"]
        )
        message_id = sender.send(frame)
        # Receiver has no pin yet, so it must reject at least one connection.
        time.sleep(0.4)
        receiver.configure_peer(
            first.ready["endpoint_id"], first.ready["endpoint_addr"]
        )
        delivered_id, delivered = receiver.recv(timeout=10)
        assert (delivered_id, delivered) == (message_id, frame)
        receiver.ack(delivered_id)
    finally:
        sender.close()
        receiver.close()


def test_unacked_delivery_survives_local_client_reconnect(sidecars) -> None:
    first, second = sidecars
    sender, receiver = configure_pair(first, second)
    frame = GOLDEN.read_bytes()
    message_id = sender.send(frame)
    delivered_id, delivered = receiver.recv(timeout=10)
    assert (delivered_id, delivered) == (message_id, frame)
    receiver.close()

    reconnected = second.client()
    try:
        delivered_id, delivered = reconnected.recv(timeout=10)
        assert (delivered_id, delivered) == (message_id, frame)
        reconnected.ack(delivered_id)
    finally:
        sender.close()
        reconnected.close()


def test_cancelled_pending_message_is_not_transmitted(short_root: Path) -> None:
    first = RunningSidecar(short_root / "first", b"a" * 32)
    second = RunningSidecar(short_root / "second", b"b" * 32)
    sender = first.client()
    receiver = second.client()
    try:
        message_id = sender.send(GOLDEN.read_bytes())
        sender.cancel(message_id)
        sender.configure_peer(second.ready["endpoint_id"], second.ready["endpoint_addr"])
        receiver.configure_peer(first.ready["endpoint_id"], first.ready["endpoint_addr"])
        with pytest.raises(TimeoutError):
            receiver.recv(timeout=1)
    finally:
        sender.close()
        receiver.close()
        first.stop()
        second.stop()


def test_observability_redacts_secret_payload_and_message_ids(short_root: Path) -> None:
    secret = b"sensitive-bootstrap-secret-32!!!"
    assert len(secret) == 32
    sidecar = RunningSidecar(short_root / "only", secret)
    client = sidecar.client()
    message_id = client.send(GOLDEN.read_bytes())
    client.cancel(message_id)
    client.close()
    time.sleep(0.1)
    logs = sidecar.stop()
    assert secret.hex() not in logs
    assert message_id.hex() not in logs
    assert "request_id" not in logs


def test_authenticated_response_failure_poisons_local_session() -> None:
    client_socket, server_socket = socket.socketpair()
    client = SidecarClient("/unused", b"z" * 32)
    client._socket = client_socket
    client._send_key = bytearray(b"s" * 32)
    client._receive_key = bytearray(b"r" * 32)

    def send_tampered_response() -> None:
        try:
            prefix = server_socket.recv(4)
            assert len(prefix) == 4
            request_size = struct.unpack(">I", prefix)[0]
            received = bytearray()
            while len(received) < request_size:
                chunk = server_socket.recv(request_size - len(received))
                assert chunk
                received.extend(chunk)
            response = bytearray(
                sidecar_client_module._encode_record(
                    sidecar_client_module._ACK,
                    0,
                    sidecar_client_module._ZERO_ID,
                    b"",
                    b"r" * 32,
                )
            )
            response[-1] ^= 1
            server_socket.sendall(struct.pack(">I", len(response)) + response)
        finally:
            server_socket.close()

    responder = threading.Thread(target=send_tampered_response, daemon=True)
    responder.start()
    with pytest.raises(AuthenticationError, match="invalid_record_tag"):
        client.ping()
    responder.join(timeout=2)
    assert not responder.is_alive()
    assert client.connected is False
    assert client._send_key is None
    assert client._receive_key is None


def test_unknown_and_terminal_cancellation_fail_explicitly(short_root: Path) -> None:
    sidecar = RunningSidecar(short_root / "only", b"c" * 32)
    client = sidecar.client()
    try:
        with pytest.raises(SidecarError, match="unknown_message"):
            client.cancel(b"u" * 16)
        message_id = client.send(GOLDEN.read_bytes())
        client.cancel(message_id)
        with pytest.raises(SidecarError, match="unknown_message"):
            client.cancel(message_id)
    finally:
        client.close()
        sidecar.stop()


def test_peer_reconfiguration_does_not_retarget_queued_message(short_root: Path) -> None:
    first = RunningSidecar(short_root / "first", b"a" * 32)
    second = RunningSidecar(short_root / "second", b"b" * 32)
    third = RunningSidecar(short_root / "third", b"c" * 32)
    sender = first.client()
    second_receiver = second.client()
    third_receiver = third.client()
    try:
        sender.configure_peer(second.ready["endpoint_id"], second.ready["endpoint_addr"])
        message_id = sender.send(GOLDEN.read_bytes())
        time.sleep(0.2)
        sender.configure_peer(third.ready["endpoint_id"], third.ready["endpoint_addr"])
        second_receiver.configure_peer(first.ready["endpoint_id"], first.ready["endpoint_addr"])
        third_receiver.configure_peer(first.ready["endpoint_id"], first.ready["endpoint_addr"])

        delivered_id, delivered = second_receiver.recv(timeout=10)
        assert (delivered_id, delivered) == (message_id, GOLDEN.read_bytes())
        second_receiver.ack(delivered_id)
        with pytest.raises(TimeoutError):
            third_receiver.recv(timeout=0.75)
    finally:
        sender.close()
        second_receiver.close()
        third_receiver.close()
        first.stop()
        second.stop()
        third.stop()


def test_malformed_server_hello_type_fails_as_authentication_error(
    short_root: Path,
) -> None:
    socket_path = short_root / "malformed.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(os.fspath(socket_path))
    listener.listen(1)

    def serve_malformed_hello() -> None:
        connection, _ = listener.accept()
        try:
            prefix = connection.recv(4)
            assert len(prefix) == 4
            request_size = struct.unpack(">I", prefix)[0]
            received = bytearray()
            while len(received) < request_size:
                chunk = connection.recv(request_size - len(received))
                assert chunk
                received.extend(chunk)
            response = json.dumps(
                {
                    "protocol": sidecar_client_module.LOCAL_PROTOCOL,
                    "server_nonce": 7,
                    "endpoint_id": "public",
                    "server_proof": "00" * 32,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            connection.sendall(struct.pack(">I", len(response)) + response)
        finally:
            connection.close()
            listener.close()

    responder = threading.Thread(target=serve_malformed_hello, daemon=True)
    responder.start()
    client = SidecarClient(socket_path, b"z" * 32)
    with pytest.raises(AuthenticationError, match="handshake_rejected"):
        client.connect()
    responder.join(timeout=2)
    assert not responder.is_alive()
    assert client.connected is False
