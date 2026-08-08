# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authenticated synchronous client for the local iroh sidecar UDS."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import struct
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, Optional, Tuple

from mycelium_router.wire import WireError, decode_frame

LOCAL_PROTOCOL = "mycelium.iroh_sidecar.local.v1"
OPERATIONAL_MAX_FRAME_BYTES = 16 * 1024 * 1024
_CONFIRMED_GENERATION_BYTES = 8
_LOCAL_MAX_PAYLOAD_BYTES = (
    OPERATIONAL_MAX_FRAME_BYTES + 2 * _CONFIRMED_GENERATION_BYTES
)

_CLIENT_PROOF_DOMAIN = b"mycelium.iroh_sidecar.local.v1/client-proof\0"
_SERVER_PROOF_DOMAIN = b"mycelium.iroh_sidecar.local.v1/server-proof\0"
_SESSION_KEYS_INFO = b"mycelium.iroh_sidecar.local.v1/session-keys"
_CLIENT_TO_SIDECAR_INFO = b"mycelium.iroh_sidecar.local.v1/client-to-sidecar"
_SIDECAR_TO_CLIENT_INFO = b"mycelium.iroh_sidecar.local.v1/sidecar-to-client"

_RECORD_VERSION = 1
_HEADER = struct.Struct(">BBQ16sI")
_TAG_BYTES = 32
_MAX_RECORD_BYTES = _HEADER.size + _LOCAL_MAX_PAYLOAD_BYTES + _TAG_BYTES
_MAX_HELLO_BYTES = 4 * 1024

_SEND = 1
_RECEIVE = 2
_DELIVERY = 3
_ACK = 4
_CANCEL = 5
_CONFIGURE_PEER = 6
_PING = 7
_ERROR = 8
_SEND_CONFIRMED = 9
_CONFIGURE_PEERS = 10
_SEND_ROUTED = 11
_ENDPOINT_ID_BYTES = 32
_ZERO_ID = b"\0" * 16


class SidecarError(RuntimeError):
    """Base client failure with a stable sidecar error code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AuthenticationError(SidecarError):
    """Handshake, HMAC, or sequence authentication failed."""


class ProtocolError(SidecarError):
    """Malformed or unexpected sidecar protocol data."""


class QueueFull(SidecarError):
    """Sidecar queue admission failed because its fixed capacity is full."""


class SidecarClient:
    """One authenticated request/response session over a Unix-domain socket.

    The object serializes operations because local records use one monotonic
    sequence per direction. Create multiple clients when callers need parallel
    polling and sending.
    """

    def __init__(
        self,
        socket_path: os.PathLike[str] | str,
        bootstrap_secret: bytes,
        *,
        timeout: float = 5.0,
    ) -> None:
        if not isinstance(bootstrap_secret, bytes) or len(bootstrap_secret) != 32:
            raise ValueError("bootstrap secret must be exactly 32 bytes")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.socket_path = Path(socket_path)
        self._secret = bytearray(bootstrap_secret)
        self._timeout = timeout
        self._socket: Optional[socket.socket] = None
        self._send_key: Optional[bytearray] = None
        self._receive_key: Optional[bytearray] = None
        self._send_sequence = 0
        self._receive_sequence = 0
        self._lock = threading.Lock()
        self.endpoint_id: Optional[str] = None
        self._peer_generation = 0

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self, *, deadline: Optional[float] = None) -> None:
        with self._lock:
            if self._socket is not None:
                raise ProtocolError("already_connected")
            stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            _apply_socket_timeout(stream, self._timeout, deadline)
            try:
                stream.connect(os.fspath(self.socket_path))
                client_nonce = os.urandom(32)
                proof = hmac.new(
                    self._secret,
                    _CLIENT_PROOF_DOMAIN + client_nonce,
                    hashlib.sha256,
                ).hexdigest()
                hello = {
                    "protocol": LOCAL_PROTOCOL,
                    "client_nonce": client_nonce.hex(),
                    "client_proof": proof,
                }
                _write_prefixed(
                    stream,
                    _canonical_json(hello),
                    socket_timeout=self._timeout,
                    deadline=deadline,
                )
                encoded_server = _read_prefixed(
                    stream,
                    _MAX_HELLO_BYTES,
                    socket_timeout=self._timeout,
                    deadline=deadline,
                )
                server = _decode_json_object(encoded_server)
                server_nonce, endpoint_id = self._verify_server_hello(
                    server, client_nonce
                )
                send_key, receive_key = _derive_session_keys(
                    bytes(self._secret), client_nonce, server_nonce
                )
            except socket.timeout:
                stream.close()
                raise
            except (
                OSError,
                EOFError,
                ValueError,
                KeyError,
                json.JSONDecodeError,
                SidecarError,
            ) as error:
                stream.close()
                raise AuthenticationError("handshake_rejected") from error

            self._socket = stream
            self._send_key = bytearray(send_key)
            self._receive_key = bytearray(receive_key)
            self._send_sequence = 0
            self._receive_sequence = 0
            self.endpoint_id = endpoint_id

    def interrupt(self) -> None:
        """Interrupt blocked socket I/O without waiting for the request lock."""

        stream = self._socket
        if stream is None:
            return
        try:
            stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self) -> None:
        self.interrupt()
        with self._lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            _wipe(self._send_key)
            _wipe(self._receive_key)
            self._send_key = None
            self._receive_key = None
            self.endpoint_id = None

    def __enter__(self) -> "SidecarClient":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def ping(self) -> None:
        self._expect_ack(*self._request(_PING, _ZERO_ID, b""), _ZERO_ID)

    def configure_peer(
        self,
        endpoint_id: str,
        endpoint_addr: dict[str, Any],
        *,
        generation: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> None:
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise ValueError("endpoint_id must be a non-empty string")
        if not isinstance(endpoint_addr, dict):
            raise ValueError("endpoint_addr must be an object")
        selected_generation = (
            self._peer_generation + 1 if generation is None else generation
        )
        if (
            not isinstance(selected_generation, int)
            or isinstance(selected_generation, bool)
            or selected_generation <= 0
        ):
            raise ValueError("generation must be a positive integer")
        payload = _canonical_json(
            {
                "endpoint_id": endpoint_id,
                "endpoint_addr": endpoint_addr,
                "generation": selected_generation,
            }
        )
        message_id = os.urandom(16)
        deadline = None if timeout is None else time.monotonic() + timeout
        self._expect_ack(
            *self._request(
                _CONFIGURE_PEER,
                message_id,
                payload,
                socket_timeout=timeout,
                deadline=deadline,
            ),
            message_id,
        )
        self._peer_generation = selected_generation

    def configure_peers(
        self,
        peers: Sequence[Mapping[str, Any]],
        *,
        timeout: Optional[float] = None,
    ) -> None:
        """Atomically install a routed peer set.

        Each entry is a mapping of ``endpoint_id``, ``endpoint_addr`` and an
        optional ``generation``.  The sidecar validates and generation-fences
        the whole set before installing any binding, so a rejected call leaves
        the previously configured table serving traffic unchanged.  The first
        entry becomes the primary outbound peer.
        """
        if isinstance(peers, (str, bytes, Mapping)):
            raise ValueError("peers must be a sequence of peer objects")
        entries = list(peers)
        if not entries:
            raise ValueError("peers must not be empty")
        default_generation = self._peer_generation + 1
        documents = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("each peer must be an object")
            endpoint_id = entry.get("endpoint_id")
            endpoint_addr = entry.get("endpoint_addr")
            if not isinstance(endpoint_id, str) or not endpoint_id:
                raise ValueError("endpoint_id must be a non-empty string")
            if not isinstance(endpoint_addr, dict):
                raise ValueError("endpoint_addr must be an object")
            if endpoint_id in seen:
                raise ValueError("peers must not repeat an endpoint_id")
            seen.add(endpoint_id)
            generation = entry.get("generation", default_generation)
            if (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation <= 0
            ):
                raise ValueError("generation must be a positive integer")
            documents.append(
                {
                    "endpoint_id": endpoint_id,
                    "endpoint_addr": endpoint_addr,
                    "generation": generation,
                }
            )

        payload = _canonical_json({"peers": documents})
        message_id = os.urandom(16)
        deadline = None if timeout is None else time.monotonic() + timeout
        self._expect_ack(
            *self._request(
                _CONFIGURE_PEERS,
                message_id,
                payload,
                socket_timeout=timeout,
                deadline=deadline,
            ),
            message_id,
        )
        # The primary binding drives generation-fenced sends, mirroring the
        # single-peer path so send_confirmed keeps authenticating correctly.
        self._peer_generation = documents[0]["generation"]

    def send(self, frame: bytes, message_id: Optional[bytes] = None) -> bytes:
        if not isinstance(frame, bytes):
            raise TypeError("Router frame must be bytes")
        if len(frame) > OPERATIONAL_MAX_FRAME_BYTES:
            raise ValueError("Router frame exceeds 16 MiB operational cap")
        try:
            decode_frame(frame)
        except (WireError, ValueError, TypeError) as error:
            raise ValueError("canonical Router ingress rejected frame") from error
        if message_id is None:
            message_id = os.urandom(16)
        _validate_message_id(message_id)
        self._expect_ack(*self._request(_SEND, message_id, frame), message_id)
        return message_id

    def send_confirmed(
        self,
        frame: bytes,
        message_id: Optional[bytes] = None,
        *,
        timeout: Optional[float] = None,
        expected_generation: Optional[int] = None,
        source_generation: Optional[int] = None,
    ) -> bytes:
        """Wait for authenticated remote Router-dispatch acknowledgement.

        ``send`` confirms bounded local-sidecar admission. This stronger operation
        pins the authenticated expected peer generation and completes only after
        the remote Python adapter ACKs successful Router dispatch. The separately
        bound source generation lets asymmetric membership generations authenticate
        correctly at the remote peer. Confirmation remains process-lifetime state;
        no sidecar disk spool exists.
        """
        if not isinstance(frame, bytes):
            raise TypeError("Router frame must be bytes")
        if len(frame) > OPERATIONAL_MAX_FRAME_BYTES:
            raise ValueError("Router frame exceeds 16 MiB operational cap")
        try:
            decode_frame(frame)
        except (WireError, ValueError, TypeError) as error:
            raise ValueError("canonical Router ingress rejected frame") from error
        if message_id is None:
            message_id = os.urandom(16)
        _validate_message_id(message_id)
        selected_timeout = self._timeout if timeout is None else timeout
        if selected_timeout <= 0:
            raise ValueError("timeout must be positive")
        selected_generation = (
            self._peer_generation
            if expected_generation is None
            else expected_generation
        )
        if (
            not isinstance(selected_generation, int)
            or isinstance(selected_generation, bool)
            or selected_generation <= 0
            or selected_generation > (1 << 64) - 1
        ):
            raise ProtocolError("peer_generation_not_configured")
        selected_source_generation = (
            selected_generation if source_generation is None else source_generation
        )
        if (
            not isinstance(selected_source_generation, int)
            or isinstance(selected_source_generation, bool)
            or selected_source_generation <= 0
            or selected_source_generation > (1 << 64) - 1
        ):
            raise ProtocolError("source_generation_not_configured")
        transport_payload = (
            selected_generation.to_bytes(_CONFIRMED_GENERATION_BYTES, "big")
            + selected_source_generation.to_bytes(
                _CONFIRMED_GENERATION_BYTES, "big"
            )
            + frame
        )
        deadline = time.monotonic() + selected_timeout
        try:
            response = self._request(
                _SEND_CONFIRMED,
                message_id,
                transport_payload,
                socket_timeout=selected_timeout,
                deadline=deadline,
            )
        except socket.timeout as error:
            raise TimeoutError("confirmed delivery deadline") from error
        self._expect_ack(*response, message_id)
        return message_id

    def send_routed(
        self,
        destination_endpoint_id: str,
        frame: bytes,
        message_id: Optional[bytes] = None,
        *,
        timeout: Optional[float] = None,
        expected_generation: Optional[int] = None,
        source_generation: Optional[int] = None,
    ) -> bytes:
        """Dispatch a frame to a named peer in the configured routing set.

        Unlike ``send``/``send_confirmed``, which always target the primary
        binding, this addresses one member of the set installed by
        ``configure_peers``.  A destination absent from that set fails closed
        with ``peer_rotated`` rather than falling back to the primary.
        Confirmation semantics match ``send_confirmed``.
        """
        if not isinstance(destination_endpoint_id, str):
            raise TypeError("destination_endpoint_id must be a string")
        try:
            destination = bytes.fromhex(destination_endpoint_id)
        except ValueError as error:
            raise ValueError("destination_endpoint_id must be hexadecimal") from error
        if len(destination) != _ENDPOINT_ID_BYTES:
            raise ValueError("destination_endpoint_id must be 32 bytes")
        if not isinstance(frame, bytes):
            raise TypeError("Router frame must be bytes")
        if len(frame) > OPERATIONAL_MAX_FRAME_BYTES:
            raise ValueError("Router frame exceeds 16 MiB operational cap")
        try:
            decode_frame(frame)
        except (WireError, ValueError, TypeError) as error:
            raise ValueError("canonical Router ingress rejected frame") from error
        if message_id is None:
            message_id = os.urandom(16)
        _validate_message_id(message_id)
        selected_timeout = self._timeout if timeout is None else timeout
        if selected_timeout <= 0:
            raise ValueError("timeout must be positive")
        selected_generation = (
            self._peer_generation
            if expected_generation is None
            else expected_generation
        )
        if (
            not isinstance(selected_generation, int)
            or isinstance(selected_generation, bool)
            or selected_generation <= 0
            or selected_generation > (1 << 64) - 1
        ):
            raise ProtocolError("peer_generation_not_configured")
        selected_source_generation = (
            selected_generation if source_generation is None else source_generation
        )
        if (
            not isinstance(selected_source_generation, int)
            or isinstance(selected_source_generation, bool)
            or selected_source_generation <= 0
            or selected_source_generation > (1 << 64) - 1
        ):
            raise ProtocolError("source_generation_not_configured")
        transport_payload = (
            destination
            + selected_generation.to_bytes(_CONFIRMED_GENERATION_BYTES, "big")
            + selected_source_generation.to_bytes(_CONFIRMED_GENERATION_BYTES, "big")
            + frame
        )
        deadline = time.monotonic() + selected_timeout
        try:
            response = self._request(
                _SEND_ROUTED,
                message_id,
                transport_payload,
                socket_timeout=selected_timeout,
                deadline=deadline,
            )
        except socket.timeout as error:
            raise TimeoutError("confirmed delivery deadline") from error
        self._expect_ack(*response, message_id)
        return message_id

    def recv(self, *, timeout: Optional[float] = None) -> Tuple[bytes, bytes]:
        message_id, _generation, payload = self.recv_with_generation(timeout=timeout)
        return message_id, payload

    def recv_with_generation(
        self, *, timeout: Optional[float] = None
    ) -> Tuple[bytes, int, bytes]:
        maximum_wait = self._timeout if timeout is None else timeout
        if maximum_wait <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + maximum_wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no sidecar delivery before deadline")
            try:
                kind, message_id, payload = self._request(
                    _RECEIVE,
                    _ZERO_ID,
                    b"",
                    socket_timeout=max(self._timeout, remaining + 0.5),
                    deadline=deadline,
                )
            except socket.timeout as error:
                raise TimeoutError("no sidecar delivery before deadline") from error
            if kind == _DELIVERY:
                if len(payload) < _CONFIRMED_GENERATION_BYTES:
                    raise ProtocolError("missing_delivery_generation")
                generation = int.from_bytes(
                    payload[:_CONFIRMED_GENERATION_BYTES], "big"
                )
                router_frame = payload[_CONFIRMED_GENERATION_BYTES:]
                if generation <= 0:
                    raise ProtocolError("invalid_delivery_generation")
                try:
                    decode_frame(router_frame)
                except (WireError, ValueError, TypeError) as error:
                    raise ProtocolError("invalid_router_delivery") from error
                return message_id, generation, router_frame
            if kind == _ERROR and _error_code(payload) == "empty":
                continue
            self._raise_response(kind, message_id, payload, _ZERO_ID)

    def ack(self, message_id: bytes) -> None:
        _validate_message_id(message_id)
        self._expect_ack(*self._request(_ACK, message_id, b""), message_id)

    def cancel(self, message_id: bytes, *, timeout: Optional[float] = None) -> None:
        _validate_message_id(message_id)
        deadline = None if timeout is None else time.monotonic() + timeout
        self._expect_ack(
            *self._request(
                _CANCEL,
                message_id,
                b"",
                socket_timeout=timeout,
                deadline=deadline,
            ),
            message_id,
        )

    def _request(
        self,
        kind: int,
        message_id: bytes,
        payload: bytes,
        *,
        socket_timeout: Optional[float] = None,
        deadline: Optional[float] = None,
    ) -> Tuple[int, bytes, bytes]:
        if deadline is None:
            self._lock.acquire()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._lock.acquire(timeout=remaining):
                raise socket.timeout("sidecar request deadline")
        try:
            stream, send_key, receive_key = self._require_session()
            previous_timeout = stream.gettimeout()
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("sidecar request deadline")
                socket_timeout = (
                    remaining
                    if socket_timeout is None
                    else min(socket_timeout, remaining)
                )
            if socket_timeout is not None:
                stream.settimeout(socket_timeout)
            try:
                encoded = _encode_record(
                    kind, self._send_sequence, message_id, payload, send_key
                )
                _write_prefixed(
                    stream,
                    encoded,
                    socket_timeout=socket_timeout,
                    deadline=deadline,
                )
                self._send_sequence = _increment_sequence(self._send_sequence)
                response = _read_prefixed(
                    stream,
                    _MAX_RECORD_BYTES,
                    socket_timeout=socket_timeout,
                    deadline=deadline,
                )
                decoded = _decode_record(
                    response, receive_key, self._receive_sequence
                )
                self._receive_sequence = _increment_sequence(self._receive_sequence)
                return decoded
            except socket.timeout:
                self._invalidate_session()
                raise
            except (AuthenticationError, ProtocolError):
                self._invalidate_session()
                raise
            except (OSError, EOFError) as error:
                self._invalidate_session()
                raise ProtocolError("sidecar_disconnected") from error
            finally:
                if self._socket is stream:
                    stream.settimeout(previous_timeout)
        finally:
            self._lock.release()

    def _verify_server_hello(
        self, server: dict[str, Any], client_nonce: bytes
    ) -> Tuple[bytes, str]:
        if set(server) != {
            "protocol",
            "server_nonce",
            "endpoint_id",
            "server_proof",
        }:
            raise ValueError("unexpected server hello fields")
        if server["protocol"] != LOCAL_PROTOCOL:
            raise ValueError("unknown local protocol")
        if not isinstance(server["server_nonce"], str) or not isinstance(
            server["server_proof"], str
        ):
            raise ValueError("invalid server hello encoding")
        endpoint_id = server["endpoint_id"]
        if not isinstance(endpoint_id, str):
            raise ValueError("invalid endpoint id")
        server_nonce = bytes.fromhex(server["server_nonce"])
        supplied = bytes.fromhex(server["server_proof"])
        if len(server_nonce) != 32 or len(supplied) != 32:
            raise ValueError("invalid server hello length")
        encoded_endpoint = endpoint_id.encode("utf-8")
        expected = hmac.new(
            self._secret,
            _SERVER_PROOF_DOMAIN
            + client_nonce
            + server_nonce
            + struct.pack(">I", len(encoded_endpoint))
            + encoded_endpoint,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("invalid server proof")
        return server_nonce, endpoint_id

    def _expect_ack(
        self, kind: int, returned_id: bytes, payload: bytes, expected_id: bytes
    ) -> None:
        if kind == _ACK and returned_id == expected_id and payload == b"":
            return
        self._raise_response(kind, returned_id, payload, expected_id)

    @staticmethod
    def _raise_response(
        kind: int, returned_id: bytes, payload: bytes, expected_id: bytes
    ) -> None:
        if returned_id != expected_id:
            raise ProtocolError("response_message_id_mismatch")
        if kind != _ERROR:
            raise ProtocolError("unexpected_response_kind")
        code = _error_code(payload)
        if code == "queue_full":
            raise QueueFull(code)
        raise SidecarError(code)

    def _require_session(self) -> Tuple[socket.socket, bytes, bytes]:
        if (
            self._socket is None
            or self._send_key is None
            or self._receive_key is None
        ):
            raise ProtocolError("not_connected")
        return self._socket, bytes(self._send_key), bytes(self._receive_key)

    def _invalidate_session(self) -> None:
        if self._socket is not None:
            self._socket.close()
        self._socket = None
        _wipe(self._send_key)
        _wipe(self._receive_key)
        self._send_key = None
        self._receive_key = None
        self.endpoint_id = None


def _derive_session_keys(
    secret: bytes, client_nonce: bytes, server_nonce: bytes
) -> Tuple[bytes, bytes]:
    salt = client_nonce + server_nonce
    pseudorandom_key = hmac.new(salt, secret, hashlib.sha256).digest()
    send = _hkdf_expand(
        pseudorandom_key, _SESSION_KEYS_INFO + _CLIENT_TO_SIDECAR_INFO
    )
    receive = _hkdf_expand(
        pseudorandom_key, _SESSION_KEYS_INFO + _SIDECAR_TO_CLIENT_INFO
    )
    return send, receive


def _hkdf_expand(pseudorandom_key: bytes, info: bytes) -> bytes:
    return hmac.new(pseudorandom_key, info + b"\x01", hashlib.sha256).digest()


def _encode_record(
    kind: int, sequence: int, message_id: bytes, payload: bytes, key: bytes
) -> bytes:
    _validate_message_id(message_id)
    if len(payload) > _LOCAL_MAX_PAYLOAD_BYTES:
        raise ProtocolError("payload_too_large")
    signed = _HEADER.pack(
        _RECORD_VERSION, kind, sequence, message_id, len(payload)
    ) + payload
    return signed + hmac.new(key, signed, hashlib.sha256).digest()


def _decode_record(
    encoded: bytes, key: bytes, expected_sequence: int
) -> Tuple[int, bytes, bytes]:
    if len(encoded) < _HEADER.size + _TAG_BYTES or len(encoded) > _MAX_RECORD_BYTES:
        raise AuthenticationError("invalid_record_length")
    signed, supplied = encoded[:-_TAG_BYTES], encoded[-_TAG_BYTES:]
    expected = hmac.new(key, signed, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, supplied):
        raise AuthenticationError("invalid_record_tag")
    version, kind, sequence, message_id, payload_length = _HEADER.unpack_from(signed)
    if version != _RECORD_VERSION:
        raise ProtocolError("unknown_record_version")
    if kind not in {
        _SEND,
        _RECEIVE,
        _DELIVERY,
        _ACK,
        _CANCEL,
        _CONFIGURE_PEER,
        _PING,
        _ERROR,
        _SEND_CONFIRMED,
        _CONFIGURE_PEERS,
        _SEND_ROUTED,
    }:
        raise ProtocolError("unknown_record_kind")
    if sequence != expected_sequence:
        raise AuthenticationError("invalid_sequence")
    payload = signed[_HEADER.size:]
    if payload_length != len(payload):
        raise ProtocolError("record_length_mismatch")
    return kind, message_id, payload


def _write_prefixed(
    stream: socket.socket,
    payload: bytes,
    *,
    socket_timeout: Optional[float] = None,
    deadline: Optional[float] = None,
) -> None:
    if not payload or len(payload) > _MAX_RECORD_BYTES:
        raise ProtocolError("invalid_length")
    _apply_socket_timeout(stream, socket_timeout, deadline)
    stream.sendall(struct.pack(">I", len(payload)) + payload)


def _read_prefixed(
    stream: socket.socket,
    maximum: int,
    *,
    socket_timeout: Optional[float] = None,
    deadline: Optional[float] = None,
) -> bytes:
    length = struct.unpack(
        ">I",
        _read_exact(
            stream,
            4,
            socket_timeout=socket_timeout,
            deadline=deadline,
        ),
    )[0]
    if length == 0 or length > maximum:
        raise ProtocolError("invalid_length")
    return _read_exact(
        stream,
        length,
        socket_timeout=socket_timeout,
        deadline=deadline,
    )


def _read_exact(
    stream: socket.socket,
    length: int,
    *,
    socket_timeout: Optional[float] = None,
    deadline: Optional[float] = None,
) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        _apply_socket_timeout(stream, socket_timeout, deadline)
        chunk = stream.recv(length - len(chunks))
        if not chunk:
            raise EOFError("sidecar closed connection")
        chunks.extend(chunk)
    return bytes(chunks)


def _apply_socket_timeout(
    stream: socket.socket,
    socket_timeout: Optional[float],
    deadline: Optional[float],
) -> None:
    selected = socket_timeout
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("sidecar request deadline")
        selected = remaining if selected is None else min(selected, remaining)
    if selected is not None:
        stream.settimeout(selected)


def _error_code(payload: bytes) -> str:
    try:
        decoded = _decode_json_object(payload)
    except (ValueError, json.JSONDecodeError) as error:
        raise ProtocolError("invalid_error_payload") from error
    if set(decoded) != {"code"} or not isinstance(decoded["code"], str):
        raise ProtocolError("invalid_error_payload")
    return decoded["code"]


def _decode_json_object(encoded: bytes) -> dict[str, Any]:
    decoded = json.loads(encoded.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("JSON value must be an object")
    return decoded


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _validate_message_id(message_id: bytes) -> None:
    if not isinstance(message_id, bytes) or len(message_id) != 16:
        raise ValueError("message_id must be exactly 16 bytes")


def _increment_sequence(sequence: int) -> int:
    if sequence == (1 << 64) - 1:
        raise ProtocolError("sequence_exhausted")
    return sequence + 1


def _wipe(value: Optional[bytearray]) -> None:
    if value is not None:
        value[:] = b"\0" * len(value)
