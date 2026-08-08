# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import io
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
from types import SimpleNamespace
from typing import Any

import pytest

from mycelium_iroh_sidecar import (
    AuthenticationError,
    OPERATIONAL_MAX_FRAME_BYTES,
    ProtocolError,
    SidecarClient,
    SidecarError,
)
from mycelium_iroh_sidecar import client as sidecar_client_module
from mycelium_router.wire import decode_frame, encode_frame

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
        try:
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
        finally:
            os.close(read_fd)
        try:
            self.ready = self._read_ready()
        except BaseException:
            self.stop()
            raise

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
        process = self.process
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stderr is None or process.stderr.closed:
                return ""
            return process.stderr.read()
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


@pytest.fixture
def short_root():
    root = Path(tempfile.mkdtemp(prefix="mycelium-p7-", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_running_sidecar_stop_closes_captured_child_streams() -> None:
    sidecar = object.__new__(RunningSidecar)
    stdout = io.StringIO("ready\n")
    stderr = io.StringIO("diagnostic\n")
    sidecar.__dict__["process"] = SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        poll=lambda: 0,
    )

    assert sidecar.stop() == "diagnostic\n"
    assert stdout.closed is True
    assert stderr.closed is True
    assert sidecar.stop() == ""


def test_running_sidecar_init_closes_bootstrap_fd_when_popen_fails(
    monkeypatch,
) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(os, "pipe", lambda: (read_fd, write_fd))

    def fail_popen(*_args, **_kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    try:
        with pytest.raises(OSError, match="synthetic spawn failure"):
            RunningSidecar(Path("/unused"), b"s" * 32)
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        for descriptor in (read_fd, write_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_running_sidecar_init_stops_child_when_readiness_validation_fails(
    monkeypatch,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    terminated = threading.Event()
    process = SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        poll=lambda: None,
        terminate=terminated.set,
        wait=lambda **_kwargs: 0,
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)

    def reject_ready(_self):
        raise ValueError("synthetic invalid readiness")

    monkeypatch.setattr(RunningSidecar, "_read_ready", reject_ready)
    try:
        with pytest.raises(ValueError, match="synthetic invalid readiness"):
            RunningSidecar(Path("/unused"), b"s" * 32)
        assert terminated.is_set()
        assert stdout.closed is True
        assert stderr.closed is True
    finally:
        stdout.close()
        stderr.close()


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
    assert len(frame_paths) == 11
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


def test_delivery_exposes_authenticated_peer_generation(sidecars) -> None:
    first, second = sidecars
    sender, receiver = configure_pair(first, second)
    frame = GOLDEN.read_bytes()
    message_id = b"g" * 16
    try:
        sender.send(frame, message_id)
        delivered_id, generation, delivered = receiver.recv_with_generation(timeout=10)
        assert (delivered_id, generation, delivered) == (message_id, 1, frame)
        receiver.ack(message_id)
    finally:
        sender.close()
        receiver.close()


def test_confirmed_send_waits_for_remote_adapter_ack(sidecars) -> None:
    first, second = sidecars
    sender, receiver = configure_pair(first, second)
    frame = GOLDEN.read_bytes()
    message_id = b"c" * 16
    outcome: list[bytes | BaseException] = []

    def send_confirmed() -> None:
        try:
            outcome.append(
                sender.send_confirmed(frame, message_id, timeout=10)
            )
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=send_confirmed)
    thread.start()
    try:
        delivered_id, delivered = receiver.recv(timeout=10)
        assert (delivered_id, delivered) == (message_id, frame)
        time.sleep(0.1)
        assert thread.is_alive()
        assert outcome == []

        receiver.ack(delivered_id)
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert outcome == [message_id]
    finally:
        sender.close()
        receiver.close()


def test_confirmed_send_carries_distinct_source_membership_generation(sidecars) -> None:
    first, second = sidecars
    sender = first.client()
    receiver = second.client()
    sender.configure_peer(
        second.ready["endpoint_id"], second.ready["endpoint_addr"], generation=2
    )
    receiver.configure_peer(
        first.ready["endpoint_id"], first.ready["endpoint_addr"], generation=1
    )
    frame = GOLDEN.read_bytes()
    message_id = b"s" * 16
    outcome: list[bytes | BaseException] = []

    def send_confirmed() -> None:
        try:
            outcome.append(
                sender.send_confirmed(
                    frame,
                    message_id,
                    timeout=10,
                    expected_generation=2,
                    source_generation=1,
                )
            )
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=send_confirmed)
    thread.start()
    try:
        delivered_id, source_generation, delivered = receiver.recv_with_generation(
            timeout=10
        )
        assert (delivered_id, source_generation, delivered) == (
            message_id,
            1,
            frame,
        )
        receiver.ack(delivered_id)
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert outcome == [message_id]
    finally:
        sender.close()
        receiver.close()


def test_exact_operational_cap_frame_is_delivered_and_confirmed(sidecars) -> None:
    first, second = sidecars
    sender, receiver = configure_pair(first, second)
    header = decode_frame(GOLDEN.read_bytes()).message
    envelope_bytes = len(encode_frame(header, b""))
    payload_bytes = OPERATIONAL_MAX_FRAME_BYTES - envelope_bytes
    frame = encode_frame(header, b"x" * payload_bytes)
    while len(frame) != OPERATIONAL_MAX_FRAME_BYTES:
        payload_bytes -= len(frame) - OPERATIONAL_MAX_FRAME_BYTES
        frame = encode_frame(header, b"x" * payload_bytes)
    assert len(frame) == OPERATIONAL_MAX_FRAME_BYTES
    message_id = b"m" * 16
    outcome: list[bytes | BaseException] = []

    def send_confirmed() -> None:
        try:
            outcome.append(sender.send_confirmed(frame, message_id, timeout=10))
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=send_confirmed)
    thread.start()
    try:
        delivered_id, delivered = receiver.recv(timeout=10)
        assert delivered_id == message_id
        assert delivered == frame
        receiver.ack(delivered_id)
        thread.join(timeout=10)
        assert outcome == [message_id]
    finally:
        sender.close()
        receiver.close()


def test_replay_collision_never_inherits_pending_or_completed_confirmation(sidecars) -> None:
    first, second = sidecars
    sender, receiver = configure_pair(first, second)
    duplicate_sender = first.client()
    original = GOLDEN.read_bytes()
    collision = (GOLDEN_DIR / "03-manifest-locked.bin").read_bytes()
    message_id = b"r" * 16
    outcome: list[bytes | BaseException] = []

    def send_original() -> None:
        try:
            outcome.append(sender.send_confirmed(original, message_id, timeout=10))
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=send_original)
    thread.start()
    try:
        delivered_id, delivered = receiver.recv(timeout=10)
        assert (delivered_id, delivered) == (message_id, original)

        with pytest.raises(SidecarError, match="replay_collision"):
            duplicate_sender.send_confirmed(
                collision,
                message_id,
                timeout=1,
                expected_generation=1,
            )

        receiver.ack(delivered_id)
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert outcome == [message_id]

        with pytest.raises(SidecarError, match="replay_collision"):
            duplicate_sender.send_confirmed(
                collision,
                message_id,
                timeout=1,
                expected_generation=1,
            )
        with pytest.raises(TimeoutError):
            receiver.recv(timeout=0.5)
    finally:
        sender.close()
        duplicate_sender.close()
        receiver.close()


def test_timed_out_confirmed_sends_release_local_session_capacity(
    short_root: Path,
) -> None:
    sidecar = RunningSidecar(short_root / "only", b"l" * 32, queue_capacity=32)
    unreachable_peer = RunningSidecar(short_root / "unreachable-peer", b"u" * 32)
    admin = sidecar.client()
    try:
        admin.configure_peer(
            unreachable_peer.ready["endpoint_id"],
            unreachable_peer.ready["endpoint_addr"],
            generation=1,
        )
        admin.close()
        for index in range(16):
            client = SidecarClient(sidecar.socket_path, sidecar.secret, timeout=0.25)
            client.connect()
            try:
                with pytest.raises(TimeoutError, match="confirmed delivery deadline"):
                    client.send_confirmed(
                        GOLDEN.read_bytes(),
                        index.to_bytes(16, "big"),
                        timeout=0.05,
                        expected_generation=1,
                    )
            finally:
                client.close()

        survivor = SidecarClient(sidecar.socket_path, sidecar.secret, timeout=2.0)
        survivor.connect()
        try:
            survivor.ping()
        finally:
            survivor.close()
    finally:
        admin.close()
        sidecar.stop()
        unreachable_peer.stop()


def test_rotation_fences_frames_queued_before_initial_peer_binding(
    short_root: Path,
) -> None:
    sender_sidecar = RunningSidecar(short_root / "sender", b"s" * 32)
    first_peer = RunningSidecar(short_root / "first-peer", b"f" * 32)
    replacement_peer = RunningSidecar(short_root / "replacement-peer", b"r" * 32)
    sender = sender_sidecar.client()
    first_receiver = first_peer.client()
    replacement_receiver = replacement_peer.client()
    try:
        first_id = sender.send(GOLDEN.read_bytes(), b"1" * 16)
        second_id = sender.send(GOLDEN.read_bytes(), b"2" * 16)
        first_receiver.configure_peer(
            sender_sidecar.ready["endpoint_id"],
            sender_sidecar.ready["endpoint_addr"],
            generation=1,
        )
        replacement_receiver.configure_peer(
            sender_sidecar.ready["endpoint_id"],
            sender_sidecar.ready["endpoint_addr"],
            generation=2,
        )
        sender.configure_peer(
            first_peer.ready["endpoint_id"],
            first_peer.ready["endpoint_addr"],
            generation=1,
        )
        delivered_id, _ = first_receiver.recv(timeout=10)
        assert delivered_id == first_id

        sender.configure_peer(
            replacement_peer.ready["endpoint_id"],
            replacement_peer.ready["endpoint_addr"],
            generation=2,
        )
        with pytest.raises(TimeoutError):
            replacement_receiver.recv(timeout=1)
        with pytest.raises(SidecarError, match="unknown_message"):
            sender.cancel(second_id)
    finally:
        sender.close()
        first_receiver.close()
        replacement_receiver.close()
        sender_sidecar.stop()
        first_peer.stop()
        replacement_peer.stop()


def test_confirmed_send_fails_if_sender_sidecar_crashes_before_confirmation(
    sidecars,
) -> None:
    first, second = sidecars
    sender, receiver = configure_pair(first, second)
    outcome: list[bytes | BaseException] = []

    def send_confirmed() -> None:
        try:
            outcome.append(
                sender.send_confirmed(GOLDEN.read_bytes(), b"x" * 16, timeout=10)
            )
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=send_confirmed)
    thread.start()
    try:
        delivered_id, _ = receiver.recv(timeout=10)
        assert delivered_id == b"x" * 16
        first.stop()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], ProtocolError)
        assert outcome[0].code == "sidecar_disconnected"
    finally:
        sender.close()
        receiver.close()


def test_peer_generation_rotation_fences_inflight_remote_delivery(sidecars) -> None:
    first, second = sidecars
    sender, receiver = configure_pair(first, second)
    frame = GOLDEN.read_bytes()
    message_id = b"g" * 16
    outcome: list[bytes | BaseException] = []

    def send_confirmed() -> None:
        try:
            outcome.append(sender.send_confirmed(frame, message_id, timeout=10))
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=send_confirmed)
    thread.start()
    try:
        delivered_id, delivered = receiver.recv(timeout=10)
        assert (delivered_id, delivered) == (message_id, frame)
        receiver.configure_peer(
            first.ready["endpoint_id"],
            first.ready["endpoint_addr"],
            generation=2,
        )

        thread.join(timeout=10)
        assert not thread.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], SidecarError)
        assert outcome[0].code == "peer_rotated"
        with pytest.raises(TimeoutError):
            receiver.recv(timeout=0.75)
    finally:
        sender.close()
        receiver.close()


def test_confirmed_send_cannot_retarget_across_generation_rotation(sidecars) -> None:
    first, second = sidecars
    sender, receiver = configure_pair(first, second)
    try:
        receiver.configure_peer(
            first.ready["endpoint_id"],
            first.ready["endpoint_addr"],
            generation=2,
        )
        sender.configure_peer(
            second.ready["endpoint_id"],
            second.ready["endpoint_addr"],
            generation=2,
        )
        with pytest.raises(SidecarError, match="peer_rotated"):
            sender.send_confirmed(
                GOLDEN.read_bytes(),
                b"z" * 16,
                timeout=1,
                expected_generation=1,
            )
        with pytest.raises(TimeoutError):
            receiver.recv(timeout=0.5)
    finally:
        sender.close()
        receiver.close()


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
