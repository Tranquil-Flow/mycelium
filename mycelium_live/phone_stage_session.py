# SPDX-License-Identifier: AGPL-3.0-only
"""Stage-only phone control through an existing, authenticated host sidecar.

One fresh local sidecar client per command avoids head-of-line blocking between
health/cancellation and an outstanding control stream. This never launches a
process, owns an endpoint, changes peer bindings or retries an uncertain command.
The endpoint owner must install the phone peer at its seed-authorized generation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
import json
import math
import os
import stat
from pathlib import Path
import re
import threading
import time
from typing import Any

from mycelium_deadlines import PHONE_REQUEST_EXECUTION_MAX_MS
from mycelium_iroh_sidecar.client import SidecarClient
from mycelium_qualification.evidence import canonical_json_bytes
from physical_inference_qualification import ControllerError, PeerIdentity, QualificationController

_PROTOCOL = 'mycelium.physical_node_control.v1'
_MAX_BYTES = 4 * 1024 * 1024
_MAX_SECONDS = 60.0
_EVENTS = {
    'configure': 'configured', 'start': 'started', 'health': 'health',
    'snapshot': 'snapshot', 'bind_request_control': 'request_control_bound',
    'update_request_control': 'request_control_updated',
    'infer_cancel_wait': 'inference_cancelled', 'stop': 'stopping',
}


#: Replies that carry no signature. ``stage_load`` is the phone's current
#: stage-load count as a HINT for the first configure; it is never evidence.
#: The only authority for a load generation is the signed configured record.
_UNSIGNED = frozenset({'hello', 'stage_load'})


def _execution_budget_ms(control: Any) -> int:
    """Translate only the budget; the opaque host subject stays byte-identical."""
    deadline = control.get("absolute_deadline_ms") if isinstance(control, Mapping) else None
    if type(deadline) is not int or deadline <= 0:
        raise ValueError("phone_execution_budget_invalid")
    remaining = int(deadline - time.monotonic() * 1000)
    if remaining <= 0:
        raise TimeoutError("request_execution_deadline_exceeded")
    if remaining > PHONE_REQUEST_EXECUTION_MAX_MS:
        raise ValueError("phone_execution_budget_invalid")
    return remaining


def validate_phone_configure(value: Any) -> dict[str, Any]:
    """The phone accepts its applied seed assignment, never controller paths."""
    if (not isinstance(value, Mapping)
            or set(value) != {'assignment_id', 'stage_id', 'pack_digest', 'manifest_digest', 'load_generation'}
            or any(not isinstance(value.get(k), str) or not value[k]
                   for k in ('assignment_id', 'stage_id'))
            or any(not isinstance(value.get(k), str)
                   or re.fullmatch(r'sha256:[0-9a-f]{64}', value[k]) is None
                   for k in ('pack_digest', 'manifest_digest'))
            or type(value.get('load_generation')) is not int or value['load_generation'] <= 0):
        raise ControllerError('run_plan_phone_configure_invalid')
    return dict(value)


def require_cancel_receipt(observation: Mapping[str, Any], subject: Mapping[str, Any]) -> None:
    """Require the exact signed cancellation generation, including at startup.

    The caller MUST already have verified this observation. Do not accept a
    transport failure, a PathCancellation ACK or an unsigned capability flag.
    """
    details = observation.get('details')
    receipt = details.get('request_cleanup') if isinstance(details, Mapping) else None
    if (not isinstance(receipt, Mapping)
            or any(type(receipt.get(k)) is not type(v) or receipt.get(k) != v for k, v in subject.items())
            or any(receipt.get(k) is not True for k in
                   ('runtime_clean', 'transport_clean', 'cancellation_worker_complete', 'complete'))):
        raise ControllerError('external_node_cancel_receipt_unsupported')


class PhoneStageSession:
    """The route session interface with seed-pinned observation verification."""

    def __init__(self, *, argv: tuple[str, ...], node_id: str, run_id: str,
                 deployment_id: str, timeout_seconds: float, peer: PeerIdentity,
                 endpoint_id: str, member_key_digest: str,
                 controller_endpoint_id: str, client_factory: Callable[..., Any]) -> None:
        if argv or peer.process_transport != 'external_iroh' or peer.node_id != node_id:
            raise ControllerError('external_session_identity_invalid')
        if (re.fullmatch(r'[0-9a-f]{64}', endpoint_id) is None
                or re.fullmatch(r'[0-9a-f]{64}', controller_endpoint_id) is None
                or re.fullmatch(r'sha256:[0-9a-f]{64}', member_key_digest) is None):
            raise ControllerError('external_session_identity_invalid')
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('phone_session_timeout_invalid')
        self.node_id = node_id
        self._peer = peer
        self._run_id = run_id
        self._deployment_id = deployment_id
        self._endpoint_id = endpoint_id
        self._controller_endpoint_id = controller_endpoint_id
        self._member_key_digest = member_key_digest
        self._timeout = min(timeout_seconds, _MAX_SECONDS)
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._closed = False
        self._inflight: dict[str, str | None] = {}
        self._clients: dict[str, Any] = {}
        self._identity: dict[str, Any] | None = None
        # These validators have no dependency on controller instance state.
        self._validator = object.__new__(QualificationController)

    @property
    def returncode(self) -> None:
        # A local connection failure is not authenticated remote process exit.
        return None

    @property
    def stderr(self) -> bytes:
        return b''

    def send(self, *, command_id: str, command: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.send_before(command_id=command_id, command=command, payload=payload,
                                deadline_monotonic_s=time.monotonic() + self._timeout)

    def send_before(self, *, command_id: str, command: str, payload: Mapping[str, Any],
                    deadline_monotonic_s: float) -> dict[str, Any]:
        if not math.isfinite(deadline_monotonic_s):
            raise ValueError('phone_session_deadline_invalid')
        deadline = min(deadline_monotonic_s, time.monotonic() + self._timeout)
        if deadline <= time.monotonic():
            raise TimeoutError('command_deadline_exceeded')
        if command not in _UNSIGNED and command not in _EVENTS:
            raise ControllerError('stage_only_node')
        if not isinstance(command_id, str) or not command_id or not isinstance(payload, Mapping):
            raise ValueError('phone_command_invalid')
        wire_payload = dict(payload)
        if command == 'bind_request_control':
            if set(wire_payload) != {'request_id', 'control'}:
                raise ValueError('phone_bind_payload_invalid')
            wire_payload['execution_budget_ms'] = _execution_budget_ms(wire_payload['control'])
        raw = canonical_json_bytes(dict(protocol=_PROTOCOL, command_id=command_id,
            run_id=self._run_id, deployment_id=self._deployment_id, command=command, payload=wire_payload))
        if len(raw) > _MAX_BYTES:
            raise ValueError('phone_command_too_large')
        with self._lock:
            if self._closed:
                raise RuntimeError('phone_session_closed')
            if command_id in self._inflight:
                raise RuntimeError('phone_command_already_inflight')
            if len(self._inflight) >= 16:
                raise RuntimeError('phone_control_capacity_exceeded')
            if command != 'hello' and self._identity is None:
                raise RuntimeError('phone_hello_required')
            self._inflight[command_id] = None
        client = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('command_deadline_exceeded')
            client = self._client_factory(timeout=remaining)
            with self._lock:
                self._clients[command_id] = client
                if self._closed:
                    raise RuntimeError('phone_session_closed')
                if self._inflight[command_id] is not None:
                    raise RuntimeError(self._inflight[command_id])
            if not callable(getattr(client, 'control_call', None)):
                raise ControllerError('external_control_carrier_required')
            client.connect(deadline=deadline)
            with self._lock:
                if self._closed:
                    raise RuntimeError('phone_session_closed')
                if self._inflight[command_id] is not None:
                    raise RuntimeError(self._inflight[command_id])
            if client.endpoint_id != self._controller_endpoint_id:
                raise ControllerError('external_controller_endpoint_mismatch')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('command_deadline_exceeded')
            if command == 'bind_request_control':
                # Connection setup consumes the host budget. Refresh from the
                # frozen serialized subject, never mutable caller data.
                request = json.loads(raw)
                request['payload']['execution_budget_ms'] = _execution_budget_ms(request['payload']['control'])
                raw = canonical_json_bytes(request)
            result = client.control_call(peer_endpoint_id=self._endpoint_id,
                request_json=raw.decode('utf-8'), timeout_seconds=remaining)
            if time.monotonic() >= deadline:
                raise TimeoutError('command_deadline_exceeded')
            if (not isinstance(result, Mapping)
                    or set(result) != {'peer_endpoint_id', 'response_json'}
                    or result['peer_endpoint_id'] != self._endpoint_id
                    or not isinstance(result['response_json'], str)):
                raise ControllerError('phone_response_invalid')
            try:
                response_bytes = result['response_json'].encode('utf-8')
                if not 0 < len(response_bytes) <= _MAX_BYTES:
                    raise ValueError
                response = json.loads(response_bytes)
                if not isinstance(response, dict) or canonical_json_bytes(response) != response_bytes:
                    raise ValueError
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ControllerError('phone_response_invalid') from exc
            if (set(response) != {'protocol', 'command_id', 'node_id', 'ok', 'route_ready',
                                  'result' if response.get('ok') is True else 'error'}
                    or response.get('protocol') != _PROTOCOL
                    or response.get('command_id') != command_id
                    or response.get('node_id') != self.node_id
                    or type(response.get('ok')) is not bool
                    or response.get('route_ready') is not False):
                raise ControllerError('phone_response_identity_invalid')
            with self._lock:
                if self._closed:
                    raise RuntimeError('phone_session_closed')
                if self._inflight[command_id] is not None:
                    raise RuntimeError(self._inflight[command_id])
            if command == 'hello':
                identity = self._validator._hello_identity(response, peer=self._peer,
                    run_id=self._run_id, deployment_id=self._deployment_id)
                if identity['endpoint_id'] != self._endpoint_id:
                    raise ControllerError('phone_hello_endpoint_mismatch')
                with self._lock:
                    self._identity = identity
            elif command == 'stage_load':
                hint = response.get('result') if response['ok'] is True else None
                if response['ok'] is True and (not isinstance(hint, Mapping)
                        or set(hint) != {'protocol', 'stage_load_generation'}
                        or hint['protocol'] != _PROTOCOL
                        or not (hint['stage_load_generation'] is None
                                or (type(hint['stage_load_generation']) is int
                                    and hint['stage_load_generation'] > 0))):
                    raise ControllerError('phone_stage_load_hint_invalid')
            elif response['ok'] is True:
                signed = response.get('result')
                if command == 'stop' and isinstance(signed, Mapping):
                    signed = signed.get('final_observation')
                key = signed.get('verification_key') if isinstance(signed, Mapping) else None
                if not isinstance(key, Mapping) or key.get('verification_key_digest') != self._member_key_digest:
                    raise ControllerError('external_member_key_mismatch')
                self._validator._verified_observation(dict(response, result=signed),
                    event=_EVENTS[command], peer=self._peer, process_id=self._peer.boot_id,
                    run_id=self._run_id, deployment_id=self._deployment_id,
                    endpoint_id=self._endpoint_id, expected_verification_key=key)
            # Preserve refusals and signed receipts byte-for-byte semantically;
            # the route owns exact request cleanup acceptance, never this carrier.
            if time.monotonic() >= deadline:
                raise TimeoutError('command_deadline_exceeded')
            return response
        except Exception:
            with self._lock:
                if self._closed:
                    raise RuntimeError('phone_session_closed') from None
                if self._inflight[command_id] is not None:
                    raise RuntimeError(self._inflight[command_id]) from None
            raise
        finally:
            try:
                if client is not None:
                    client.close()
            finally:
                with self._lock:
                    self._inflight.pop(command_id, None)
                    self._clients.pop(command_id, None)

    def interrupt_command(self, command_id: str, *, code: str) -> bool:
        # Retire the reply, not the shared remote process or its cleanup state.
        with self._lock:
            if command_id not in self._inflight:
                return False
            self._inflight[command_id] = code
            client = self._clients.get(command_id)
        if client is not None:
            client.interrupt()
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            clients = tuple(self._clients.values())
        for client in clients:
            client.interrupt()


class PhoneStageSessionFactory:
    """Bind a route to an already-running host endpoint, with no argv authority.

    The bootstrap secret authenticates only the LOCAL sidecar protocol. The
    controller endpoint is authenticated by iroh; its authorization comes from
    the seed-signed phone assignment, not a network address or this secret.
    """

    def __init__(self, *, socket_path: Any, bootstrap_secret: bytes,
                 controller_endpoint_id: str) -> None:
        self._client_factory = partial(SidecarClient, socket_path, bootstrap_secret)
        self._controller_endpoint_id = controller_endpoint_id

    @classmethod
    def from_descriptor(cls, descriptor: Any) -> "PhoneStageSessionFactory":
        """Attach to an operator-owned endpoint; no launch or peer authority.

        The bootstrap file is local secret material, never put in the plan or
        forwarded to a phone. Refuse symlinks and group/world access.
        """
        try:
            if (not isinstance(descriptor, Mapping) or set(descriptor) != {
                    'socket_path', 'bootstrap_secret_file', 'controller_endpoint_id'}
                    or any(not isinstance(v, str) or not v for v in descriptor.values())
                    or re.fullmatch(r'[0-9a-f]{64}', descriptor['controller_endpoint_id']) is None
                    or not Path(descriptor['socket_path']).is_absolute()
                    or not Path(descriptor['bootstrap_secret_file']).is_absolute()):
                raise ValueError
            fd = os.open(descriptor['bootstrap_secret_file'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                meta = os.fstat(fd)
                if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                        or stat.S_IMODE(meta.st_mode) & 0o077 or meta.st_size != 32):
                    raise ValueError
                secret = os.read(fd, 33)
                if len(secret) != 32:
                    raise ValueError
            finally:
                os.close(fd)
            return cls(socket_path=Path(descriptor['socket_path']), bootstrap_secret=secret,
                       controller_endpoint_id=descriptor['controller_endpoint_id'])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ControllerError('external_control_binding_invalid') from exc

    def __call__(self, *, peer: PeerIdentity, endpoint_id: str,
                 member_key_digest: str, **kwargs: Any) -> PhoneStageSession:
        return PhoneStageSession(peer=peer, endpoint_id=endpoint_id,
            member_key_digest=member_key_digest, controller_endpoint_id=self._controller_endpoint_id,
            client_factory=self._client_factory, **kwargs)
