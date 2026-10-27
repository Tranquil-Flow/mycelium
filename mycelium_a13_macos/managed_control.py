# SPDX-License-Identifier: AGPL-3.0-only
"""Authenticated target-initiated mailbox for bounded native node control.

This module is deliberately only a transport.  It never starts a process,
opens SSH, loads a model, or interprets command payloads.  The target polls for
one owner-signed canonical ``physical_node_control.v1`` envelope and may pass
that envelope only to the existing native controller.  The initial operation
allowlist is closed over hello/configure/start/health/snapshot/cancel/stop;
generic shell, exec, Python, argv, script, and path-launch inputs are rejected.

Production clients use Web-PKI HTTPS and a redirect-refusing urllib opener.
``LocalTLSManagedControlServer`` and ``ManagedControlClient.for_local_tls`` are
explicitly test-only/local-installation TLS seams.  Claimed work is never
blindly redelivered: a later poll exposes ``reconciliation_required`` without
the command body until the target submits an authenticated native receipt.
This avoids an exactly-once effects claim across process or host crashes.

Parent integration API:

* ``ManagedControlMailbox(path, coordinator=DemoAuthority.coordinator)``
* mount ``mailbox.handle_http(method, path, body)`` under an HTTPS origin
* owner ``ManagedControlClient.enqueue(target=coordinator.member(...), ...)``
* target ``poll(...)`` then existing native controller ``command(...)``
* target ``ack(..., native_response=<canonical physical response>)``

The durable owner signer is ``coordinator.signer``.  A target client must use
the same durable signer as ``RemoteMacMember`` and the current durable
membership projection.  No verifier-specific protocol is involved.
"""

from __future__ import annotations

from mycelium_http_identity import with_user_agent
from collections.abc import Mapping
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import secrets
from contextvars import ContextVar
import sqlite3
import ssl
import socket
import stat
import threading
import time
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_qualification.signing import (
    Ed25519EvidenceSigner,
    SIGNATURE_FIELDS,
    build_ed25519_verifier,
)
from mycelium_seed.coordinator import SeedCoordinator, SeedCoordinatorError


_REQUEST_DEADLINE = ContextVar("managed_control_request_deadline", default=None)
_REQUEST_INTERRUPTION = ContextVar("managed_control_request_interruption", default=None)


@contextmanager
def request_deadline(deadline_monotonic_s, *, interruption=None):
    """Propagate one caller budget without mutating a shared client's timeout."""
    if not math.isfinite(deadline_monotonic_s) or deadline_monotonic_s <= time.monotonic():
        raise ManagedControlError("managed_control_response_timeout")
    token = _REQUEST_DEADLINE.set(deadline_monotonic_s)
    interrupt_token = _REQUEST_INTERRUPTION.set(interruption)
    try:
        yield
    finally:
        _REQUEST_INTERRUPTION.reset(interrupt_token)
        _REQUEST_DEADLINE.reset(token)


CONTROL_COMMAND_PROTOCOL = "mycelium.physical_node_control.v1"
SIGNED_ENVELOPE_PROTOCOL = "mycelium.managed_control.signed_envelope.v1"
OWNER_COMMAND_PROTOCOL = "mycelium.managed_control.owner_command.v1"
OWNER_RESULT_PROTOCOL = "mycelium.managed_control.owner_result.v1"
RESULT_RECEIPT_PROTOCOL = "mycelium.managed_control.result_receipt.v1"
TARGET_POLL_PROTOCOL = "mycelium.managed_control.target_poll.v1"
TARGET_POLL_PROTOCOL_V2 = "mycelium.managed_control.target_poll.v2"
TARGET_ACK_PROTOCOL = "mycelium.managed_control.target_ack.v1"
ENQUEUE_RECEIPT_PROTOCOL = "mycelium.managed_control.enqueue_receipt.v1"
POLL_RECEIPT_PROTOCOL = "mycelium.managed_control.poll_receipt.v1"
ACK_RECEIPT_PROTOCOL = "mycelium.managed_control.ack_receipt.v1"

MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_TTL_SECONDS = 300.0
MAX_CLOCK_SKEW_SECONDS = 30.0
_ALLOWED_OPERATIONS = frozenset(
    {"hello", "configure", "start", "health", "snapshot", "cancel", "stop",
     "bind_request_control", "update_request_control", "infer_start", "infer_decode",
     "infer_cancel", "infer_cancel_wait", "rotate", "install_token_path"}
)
_WORK_OPERATIONS = frozenset({"configure", "start", "infer_start", "infer_decode", "rotate", "install_token_path"})
_MAX_ACTIVE_WORK = 1
_MAX_ACTIVE_CONTROL = 2
_MAX_PENDING_COMMANDS = 128
_MEMBER_STATES = frozenset(
    {"NEW", "CONFIGURED", "RUNNING", "DRAINING", "STOPPING", "STOPPED"}
)
# The states that may still RUN work -- deliberately NOT the set above, which also admits
# STOPPING and STOPPED because a stopped member still carries a verifiable binding.  A
# revocation is defined as an advance to STOPPED (DemoAuthority.revoke), so this set is what
# tells a benign re-admission from a revocation when both advance the generation.
_LIVE_MEMBER_STATES = frozenset({"NEW", "CONFIGURED", "RUNNING"})
_COMMAND_FIELDS = frozenset(
    {"protocol", "command_id", "run_id", "deployment_id", "command", "payload"}
)
_OPERATION_PAYLOAD_FIELD_SETS = {
    "hello": (frozenset(),),
    "configure": (
        frozenset(
            {"assignment_file", "artifact_report_file", "graph", "device_states", "load_generation"}
        ),
        frozenset(
            {"assignment_file", "manifest_file", "stage_pack_file", "graph", "device_states", "load_generation"}
        ),
        frozenset(
            {"assignment_file", "pixel_stage_pack_file", "graph", "device_states", "load_generation"}
        ),
    ),
    "start": (
        frozenset({"peer", "local_generation"}),
        frozenset({"peer", "peers", "local_generation"}),
    ),
    "health": (frozenset(),),
    "snapshot": (
        frozenset(),
        frozenset({"cleanup_subject"}),
        frozenset({"cleanup_subject", "receipt_only"}),
    ),
    "install_token_path": (frozenset({"policy"}),),
    "cancel": (frozenset({"request_id"}),),
    "stop": (frozenset(),),
}
_CANCEL_CONTROL_FIELDS = frozenset({"request_id", "deployment_id", "deployment_epoch",
    "qualification_digest", "command_id", "publisher_generation", "absolute_deadline_ms",
    "request_attempt", "path_id", "path_attempt", "path_digest", "topology_generation",
    "cancellation_generation", "deadline_budget_ms"})
_OPERATION_PAYLOAD_FIELD_SETS.update({
    "bind_request_control": (frozenset({"request_id", "control"}),
        frozenset({"request_id", "control", "token_path"})),
    "update_request_control": (frozenset({"request_id", "control"}),),
    "infer_start": (frozenset({"request"}), frozenset({"request", "excluded_placement_ids"}),
        frozenset({"request", "control", "path_manifest"}),
        frozenset({"request", "control", "path_manifest", "excluded_placement_ids"}),
        frozenset({"request", "control", "path_manifest", "token_grant"})),
    "infer_decode": (frozenset({"request_id", "count"}), frozenset({"request_id", "count", "control"})),
    "infer_cancel": (frozenset({"request_id"}), _CANCEL_CONTROL_FIELDS),
    "infer_cancel_wait": (_CANCEL_CONTROL_FIELDS,),
    "rotate": (frozenset({"peer"}),),
})

_ENVELOPE_FIELDS = frozenset(
    {"protocol", "statement", "signature", "verification_key"}
)
_BINDING_FIELDS = frozenset(
    {
        "target_node_id",
        "target_endpoint_id",
        "target_key_digest",
        "membership_generation",
        "member_incarnation",
        "run_id",
        "deployment_id",
    }
)
_REQUEST_COMMON_FIELDS = frozenset(
    {"protocol", "request_id", "swarm_id", "sequence", "nonce", "issued_at", "expires_at"}
)
_OWNER_FIELDS = _REQUEST_COMMON_FIELDS | _BINDING_FIELDS | frozenset(
    {"owner_node_id", "native_command"}
)
_POLL_FIELDS = _REQUEST_COMMON_FIELDS | _BINDING_FIELDS
_POLL_V2_FIELDS = _POLL_FIELDS | frozenset({"active_claims"})
_ACK_FIELDS = _POLL_FIELDS | frozenset(
    {"command_id", "claim_token", "disposition", "native_response"}
)
# Optional, authenticated target diagnostic. It is never native evidence or
# permission to retry an uncertain command. Old envelopes keep their exact shape.
_TARGET_ERROR_CODES = frozenset({
    'token_path_policy_expired', 'token_path_policy_stale', 'token_path_policy_signature',
    'token_path_policy_mismatch', 'token_path_policy_unconfigured',
    'token_path_host_pin_stale', 'token_path_host_pin_invalid', 'token_path_process_expired',
    'runtime_member_stale', 'worker_membership_not_current',
})


def _diagnostic_fields(envelope, fields):
    body = envelope.get('statement') if isinstance(envelope, Mapping) else None
    return fields | {'target_error_code'} if isinstance(body, Mapping) and 'target_error_code' in body else fields


def _target_diagnostic(statement):
    if 'target_error_code' not in statement:
        return {}
    code = statement['target_error_code']
    _require(isinstance(code, str) and code in _TARGET_ERROR_CODES
             and statement.get('disposition', statement.get('state')) == 'reconciliation_required'
             and statement.get('native_response') is None,
             'managed_control_target_diagnostic_invalid')
    return {'target_error_code': code}


_RESPONSE_COMMON_FIELDS = frozenset(
    {
        "protocol",
        "request_id",
        "swarm_id",
        "owner_node_id",
        "owner_endpoint_id",
        "issued_at",
        "expires_at",
    }
)
_ENQUEUE_RECEIPT_FIELDS = _RESPONSE_COMMON_FIELDS | _BINDING_FIELDS | frozenset(
    {"command_id", "command_digest", "state"}
)
_POLL_RECEIPT_FIELDS = _RESPONSE_COMMON_FIELDS | _BINDING_FIELDS | frozenset(
    {"delivery_kind", "command_id", "claim_token", "command_envelope"}
)
_ACK_RECEIPT_FIELDS = _RESPONSE_COMMON_FIELDS | _BINDING_FIELDS | frozenset(
    {"command_id", "state", "native_response_digest"}
)
_RESULT_RECEIPT_FIELDS = _ENQUEUE_RECEIPT_FIELDS | frozenset(
    {"native_response", "native_response_digest"}
)
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_EXECUTION_KEYS = frozenset(
    {
        "argv",
        "cwd",
        "exec",
        "executable",
        "executable_path",
        "file_path",
        "path",
        "python",
        "python_code",
        "python_command",
        "script",
        "script_path",
        "shell",
        "shell_command",
        "subprocess",
        "working_directory",
    }
)
_TERMINAL_STATES = frozenset({"completed", "failed", "expired"})


_UNREACHABLE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _bounded_unreachable_cause(error: BaseException) -> str | None:
    """Bounded, allowlisted identity for a connection-level failure.

    ``managed_control_unreachable`` is raised for every socket/TLS failure, so the
    one fact that discriminates a transient network drop from a fail-closed
    library guard (for example ``relay_dns_nonpublic`` from the public-relay DNS
    policy) was thrown away.  Only a coded token this codebase raises, or a
    generic DNS/timeout classification, is retained; arbitrary OS message text is
    never propagated.
    """

    for candidate in (error, getattr(error, "reason", None)):
        if candidate is None:
            continue
        if isinstance(candidate, socket.gaierror):
            return "dns_resolution_failed"
        text = str(candidate)
        if _UNREACHABLE_CODE.fullmatch(text) is not None:
            return text
        if isinstance(candidate, TimeoutError):
            return "connect_timeout"
    return None


class ManagedControlError(RuntimeError):
    """Stable, sanitized managed-control failure.

    ``request_path`` is an additive, allowlisted route identity (for example
    ``/managed-control/staging/read``).  It is set only where the failing HTTP
    call site is known, so a bounded private diagnostic can attribute a failure
    to one route instead of collapsing everything into a generic code.  It is
    never part of the public error body.
    """

    def __init__(self, code: str, *, status: int | None = None, request_path: str | None = None,
                 cause_code: str | None = None) -> None:
        self.code = code
        self.status = status
        self.request_path = request_path
        self.cause_code = cause_code
        super().__init__(code)


class _TransientHTTPError(ManagedControlError):
    """Unstructured proxy error only; a structured refusal never has this type."""


# Eight total HTTP attempts, one 10 s monotonic budget (or the caller's earlier
# deadline), exponential 100 ms -> 1 s backoff. Never retry arbitrary POSTs:
# these routes deduplicate signed request bytes or perform a read-only lookup.
HTTP_RETRY_ATTEMPTS = 8
HTTP_RETRY_WINDOW_SECONDS = 10.0
HTTP_RETRY_INITIAL_SECONDS = 0.1
HTTP_RETRY_MAX_SECONDS = 1.0
_HTTP_RETRY_STATUSES = frozenset({502, 503, 504, 530})
_HTTP_REPLAY_PATHS = frozenset({
    '/managed-control/enqueue', '/managed-control/poll', '/managed-control/ack',
    '/managed-control/result', '/managed-control/token-progress',
    '/managed-control/staging/read', '/managed-control/staging/cleanup/read',
})


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ManagedControlError(code)


def _segment(value: Any, code: str = "managed_control_identifier_invalid") -> str:
    _require(isinstance(value, str) and _SEGMENT_RE.fullmatch(value) is not None, code)
    return value


def _digest(value: Any, code: str = "managed_control_digest_invalid") -> str:
    _require(isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None, code)
    return value


def _integer(value: Any, code: str, *, minimum: int = 1) -> int:
    _require(type(value) is int and value >= minimum, code)
    return value


def _number(value: Any, code: str) -> float:
    _require(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value)),
        code,
    )
    return float(value)


def _document_digest(value: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(dict(value)))


def _detached(value: Any, *, code: str = "managed_control_json_invalid") -> Any:
    try:
        raw = canonical_json_bytes(value)
        _require(0 < len(raw) <= MAX_FRAME_BYTES, "managed_control_frame_too_large")
        return json.loads(raw)
    except ManagedControlError:
        raise
    except Exception as exc:
        raise ManagedControlError(code) from exc


def _validate_time(statement: Mapping[str, Any], *, now: float) -> None:
    issued = _number(statement.get("issued_at"), "managed_control_time_invalid")
    expires = _number(statement.get("expires_at"), "managed_control_time_invalid")
    current = _number(now, "managed_control_clock_invalid")
    _require(issued < expires, "managed_control_time_invalid")
    _require(expires - issued <= MAX_TTL_SECONDS, "managed_control_ttl_invalid")
    # The documented skew tolerance holds on both sides: a member clock ahead of the owner's must not
    # drop short-TTL commands (infer_cancel_wait 4.5 s).  The owner's mailbox still expires rows on its
    # own clock and the nonce/sequence ledgers still block replay.
    if current > expires + MAX_CLOCK_SKEW_SECONDS:
        raise ManagedControlError("managed_control_message_expired")
    if current + MAX_CLOCK_SKEW_SECONDS < issued:
        raise ManagedControlError("managed_control_message_from_future")


def _reject_execution_inputs(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _require(isinstance(key, str), "managed_control_native_command_invalid")
            normalized = key.lower().replace("-", "_")
            _require(
                normalized not in _FORBIDDEN_EXECUTION_KEYS,
                "managed_control_execution_input_forbidden",
            )
            _reject_execution_inputs(item)
    elif isinstance(value, list):
        for item in value:
            _reject_execution_inputs(item)


def validate_native_command(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the exact canonical native envelope and closed operation set."""

    _require(
        isinstance(value, Mapping) and set(value) == _COMMAND_FIELDS,
        "managed_control_native_command_invalid",
    )
    command = _detached(dict(value), code="managed_control_native_command_invalid")
    _require(
        command["protocol"] == CONTROL_COMMAND_PROTOCOL,
        "managed_control_native_command_invalid",
    )
    for field in ("command_id", "run_id", "deployment_id"):
        _segment(command[field], "managed_control_native_command_invalid")
    _require(
        isinstance(command["command"], str), "managed_control_native_command_invalid"
    )
    _require(
        command["command"] in _ALLOWED_OPERATIONS,
        "managed_control_operation_unsupported",
    )
    _require(isinstance(command["payload"], dict), "managed_control_native_command_invalid")
    _reject_execution_inputs(command["payload"])
    _require(
        frozenset(command["payload"]) in _OPERATION_PAYLOAD_FIELD_SETS[command["command"]],
        "managed_control_native_command_invalid",
    )
    if command["command"] == "install_token_path":
        from mycelium_node.token_provisioning import valid_policy_payload
        _require(valid_policy_payload(command["payload"]), "managed_control_native_command_invalid")
    if command["command"] == "bind_request_control" and "token_path" in command["payload"]:
        from mycelium_node.token_participants import valid_bind_token_shape
        _require(valid_bind_token_shape(command["payload"]), "managed_control_native_command_invalid")
    if command["command"] == "infer_start" and "token_grant" in command["payload"]:
        from mycelium_node.token_admission import valid_start_grant_shape
        _require(valid_start_grant_shape(command["payload"]),
                 "managed_control_native_command_invalid")
    if command["command"] == "cancel":
        _segment(
            command["payload"].get("request_id"),
            "managed_control_native_command_invalid",
        )
    return command


def sign_managed_statement(
    signer: Ed25519EvidenceSigner, statement: Mapping[str, Any]
) -> dict[str, Any]:
    """Sign canonical managed-control bytes with the repository Ed25519 primitive."""

    _require(isinstance(signer, Ed25519EvidenceSigner), "managed_control_signer_invalid")
    detached = _detached(dict(statement))
    return {
        "protocol": SIGNED_ENVELOPE_PROTOCOL,
        "statement": detached,
        "signature": signer.sign(detached),
        "verification_key": signer.public_key_record(),
    }


def _verify_signed(
    envelope: Mapping[str, Any],
    *,
    expected_protocol: str,
    expected_key_record: Mapping[str, Any],
    expected_endpoint_id: str,
    now: float,
) -> dict[str, Any]:
    _require(
        isinstance(envelope, Mapping) and set(envelope) == _ENVELOPE_FIELDS,
        "managed_control_envelope_invalid",
    )
    _require(
        envelope.get("protocol") == SIGNED_ENVELOPE_PROTOCOL,
        "managed_control_envelope_invalid",
    )
    statement = envelope.get("statement")
    signature = envelope.get("signature")
    record = envelope.get("verification_key")
    _require(
        isinstance(statement, Mapping)
        and isinstance(signature, Mapping)
        and isinstance(record, Mapping),
        "managed_control_envelope_invalid",
    )
    _require(
        statement.get("protocol") == expected_protocol,
        "managed_control_protocol_invalid",
    )
    _require(
        isinstance(expected_key_record, Mapping)
        and record.get("verification_key_digest")
        == expected_key_record.get("verification_key_digest")
        and dict(record) == dict(expected_key_record),
        "managed_control_key_pin_mismatch",
    )
    _require(
        set(signature) == SIGNATURE_FIELDS
        and signature.get("signer_endpoint_id") == expected_endpoint_id,
        "managed_control_signature_invalid",
    )
    try:
        verifier = build_ed25519_verifier([dict(expected_key_record)])
        valid = verifier(canonical_json_bytes(dict(statement)), dict(signature))
    except Exception as exc:
        raise ManagedControlError("managed_control_signature_invalid") from exc
    _require(valid, "managed_control_signature_invalid")
    _validate_time(statement, now=now)
    return _detached(dict(statement))


def _binding_from_member(target: Mapping[str, Any], run_id: str, deployment_id: str) -> dict[str, Any]:
    try:
        binding = {
            "target_node_id": target["node_id"],
            "target_endpoint_id": target["endpoint_id"],
            "target_key_digest": target["verification_key_digest"],
            "membership_generation": target["generation"],
            "member_incarnation": target["incarnation"],
            "run_id": run_id,
            "deployment_id": deployment_id,
        }
    except (KeyError, TypeError) as exc:
        raise ManagedControlError("managed_control_member_binding_invalid") from exc
    _validate_binding(binding)
    return binding


def _binding_advanced(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """True when `current` is a NEWER incarnation of the SAME member.

    Routine relay rotation re-admits the same node with a new generation and a new
    incarnation, so a durable binding captured earlier no longer compares equal.
    That is our own member coming back, not a different one, and callers rebind and
    carry on.  Round 64r1 failed closed here instead: the node's own re-admission
    killed its native worker and its control loop one second before the run-level
    configure became unknowable.

    Narrow on purpose.  A different node, endpoint, key, run or deployment is a
    DIFFERENT member, and a rewind to an older generation is never an advance.
    Both still fail closed.
    """
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return False
    for field in ("target_node_id", "target_endpoint_id", "target_key_digest",
                  "run_id", "deployment_id"):
        if previous.get(field) != current.get(field):
            return False
    before = previous.get("membership_generation")
    after = current.get("membership_generation")
    return type(before) is int and type(after) is int and after > before


def _member_live(member: Mapping[str, Any]) -> bool:
    """True when a member record is still eligible to run work.

    Needed to tell a RE-ADMISSION from a REVOCATION, which are otherwise identical in the
    binding: both advance the generation and the incarnation.  `DemoAuthority.revoke`
    advances the generation with `lifecycle_state="STOPPED"`, so 64r1's own re-admission
    (which comes back RUNNING) and a revocation are distinguishable only by that state.
    The product already treats {NEW, CONFIGURED, RUNNING} as the states that may run work
    (authority.py:689, managed_provisioning.py:449), so reusing that set keeps revocation
    fail-closed.  Note `_MEMBER_STATES` is NOT this set: it includes STOPPED, because it
    bounds which lifecycle states carry a verifiable binding.
    """
    return isinstance(member, Mapping) and member.get("lifecycle_state") in _LIVE_MEMBER_STATES


def _validate_binding(statement: Mapping[str, Any]) -> None:
    for field in (
        "target_node_id",
        "target_endpoint_id",
        "member_incarnation",
        "run_id",
        "deployment_id",
    ):
        _segment(statement.get(field), "managed_control_member_binding_invalid")
    _digest(statement.get("target_key_digest"), "managed_control_member_binding_invalid")
    _integer(
        statement.get("membership_generation"),
        "managed_control_member_binding_invalid",
    )


def _validate_request_common(statement: Mapping[str, Any]) -> None:
    for field in ("request_id", "swarm_id", "nonce"):
        _segment(statement.get(field))
    _integer(statement.get("sequence"), "managed_control_sequence_invalid")
    _validate_binding(statement)


def _validate_native_response(
    value: Any, *, command_id: str, node_id: str, disposition: str
) -> dict[str, Any]:
    _require(isinstance(value, Mapping), "managed_control_native_response_invalid")
    response = _detached(dict(value), code="managed_control_native_response_invalid")
    common = {"protocol", "command_id", "node_id", "ok", "route_ready"}
    expected = common | ({"result"} if response.get("ok") is True else {"error"})
    _require(set(response) == expected, "managed_control_native_response_invalid")
    _require(
        response.get("protocol") == CONTROL_COMMAND_PROTOCOL
        and response.get("command_id") == command_id
        and response.get("node_id") == node_id
        and response.get("route_ready") is False
        and type(response.get("ok")) is bool,
        "managed_control_native_response_invalid",
    )
    if disposition == "completed":
        _require(response["ok"] is True, "managed_control_native_response_invalid")
    else:
        _require(response["ok"] is False, "managed_control_native_response_invalid")
        error = response.get("error")
        _require(
            isinstance(error, dict)
            and set(error) == {"code"}
            and _SEGMENT_RE.fullmatch(str(error.get("code"))) is not None,
            "managed_control_native_response_invalid",
        )
    return response


def _canonical_loads(raw: bytes) -> Mapping[str, Any]:
    try:
        _require(0 < len(raw) <= MAX_FRAME_BYTES, "managed_control_frame_invalid")
        value = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            object_pairs_hook=lambda pairs: _pairs_without_duplicates(pairs),
        )
        _require(isinstance(value, Mapping), "managed_control_frame_invalid")
        _require(canonical_json_bytes(dict(value)) == raw, "managed_control_frame_noncanonical")
        return value
    except ManagedControlError:
        raise
    except Exception as exc:
        raise ManagedControlError("managed_control_frame_invalid") from exc


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


_LONG_POLL_HOLD_SECONDS = 1.5
"""How long an empty *concurrent* control poll is held open for a late command.

Measured provenance (round 52, two-Mac relayed route).  The target's control pump
performs exactly one synchronous relay HTTP operation per ``step()`` and then
sleeps 50 ms, so a command enqueued just after a poll departed waits a full cycle:
measured claim latency was **1.030 s** for the relayed laptop against **0.119 s**
for the same-machine node, whose relay round trip is ~5 ms but whose pump runs the
same 50 ms cadence.  That wait is what makes the relayed peer's cleanup
(claim ~1.0 s + transport drain ~1.0 s + receipt seal ~0.5 s) unable to fit the
route's immutable 2.0 s cancellation window, which is why cleanup could never be
proven in time no matter how the owner-side attempt slice was sized.

Holding the poll open while nothing is pending converts that wait into the return
transit alone.  It is the only lever that shortens *delivery* without touching a
proof boundary, the immutable budget, or the already-correct 50 ms cadence.

Bounded on purpose: one ``step()`` blocks for the whole hold, so a longer hold
delays this peer's own acknowledgement of completed native work by as much.
"""

_LONG_POLL_STEP_SECONDS = 0.02
"""Re-attempt interval inside the hold; the mailbox lock is released between attempts.

One empty attempt costs a measured 1.2 ms median / 1.33 ms p95 (local disk,
``synchronous = FULL``), so a 20 ms step leaves the process-wide mailbox lock free
for all but ~6 % of the hold and the whole hold costs ~5 % of one CPU's time.
"""

_LONG_POLL_MAX_HOLD_SECONDS = 5.0
"""Hard ceiling for a configurable hold: a nonsense value must fail closed at
construction rather than park a peer's control pump indefinitely."""

_DELIVERY_WAKE_PROTOCOL = "mycelium.a13.mailbox_delivery_wake.v1"
"""The in-process delivery wake-up a parked long poll waits on.

The poll loop below already re-attempts inside its hold, but it re-attempts on an
*interval* (``long_poll_step``).  An interval can only ever be as prompt as its own
period, and the measured p81r1 inversion (DIAGNOSIS-p81r1-cleanup-unproven.md) is
exactly that: the laptop peer's cleanup command was claimed at **3 957 ms of its
4 427 ms life** and then needed **3 862 ms**, so it could never complete and the route
withheld the terminal.  PLAN.md D9 prescribes the fix on this side of the inversion:
"fix the delivery path (e.g. long-poll wake-up instead of interval polling), not the
budget".  So an enqueue now *signals* the parked polls and they re-attempt at once.

This is a delivery signal only.  It never widens, relaxes or re-derives a hold, a step,
a deadline, a cleanup window, a skew or a staleness constant: the interval stays, as
the ceiling that still bounds a missed signal.
"""


def _empty_poll_delivery(response: Mapping[str, Any]) -> bool:
    """True only for a signed poll receipt that delivered nothing.

    Decides both whether the hold continues and whether a recorded reply may be
    replaced by a later delivery.  Deliberately strict: anything that is not a
    poll receipt with ``delivery_kind == 'empty'`` is treated as final, so a
    malformed or unexpected document can never extend a hold or be overwritten.
    """

    statement = response.get("statement") if isinstance(response, Mapping) else None
    return (
        isinstance(statement, Mapping)
        and statement.get("protocol") == POLL_RECEIPT_PROTOCOL
        and statement.get("delivery_kind") == "empty"
    )


from .token_receipt_mailbox import TokenProgressMailboxMixin


class ManagedControlMailbox(TokenProgressMailboxMixin):
    """SQLite-backed owner/target control mailbox with current-member guards."""

    def __init__(
        self,
        database: str | Path,
        *,
        coordinator: SeedCoordinator,
        clock: Callable[[], float] = time.time,
        claim_token_source: Callable[[], str] | None = None,
        long_poll_hold: float = _LONG_POLL_HOLD_SECONDS,
        long_poll_step: float = _LONG_POLL_STEP_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        _require(isinstance(coordinator, SeedCoordinator), "managed_control_coordinator_invalid")
        _require(callable(clock), "managed_control_clock_invalid")
        _require(
            not isinstance(long_poll_hold, bool)
            and isinstance(long_poll_hold, (int, float))
            and math.isfinite(float(long_poll_hold))
            and 0.0 <= long_poll_hold <= _LONG_POLL_MAX_HOLD_SECONDS,
            "managed_control_long_poll_hold_invalid",
        )
        _require(
            not isinstance(long_poll_step, bool)
            and isinstance(long_poll_step, (int, float))
            and math.isfinite(float(long_poll_step))
            and 0.0 < long_poll_step <= _LONG_POLL_MAX_HOLD_SECONDS,
            "managed_control_long_poll_step_invalid",
        )
        _require(callable(monotonic), "managed_control_monotonic_invalid")
        if claim_token_source is None:
            claim_token_source = lambda: "claim-" + secrets.token_hex(24)
        _require(callable(claim_token_source), "managed_control_claim_source_invalid")
        self.coordinator = coordinator
        self.owner_signer = coordinator.signer
        self.database = Path(os.path.abspath(Path(database).expanduser()))
        self._clock_source = clock
        self._claim_token_source = claim_token_source
        self.long_poll_hold = float(long_poll_hold)
        self.long_poll_step = float(long_poll_step)
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._closed = False
        self._setup_token_progress()
        # Delivery wake-up (see _DELIVERY_WAKE_PROTOCOL).  A monotonically increasing
        # generation, so a waiter can tell "an enqueue signalled me" from "my interval
        # elapsed" without holding the mailbox lock while it waits.
        self._delivery_wakeup = threading.Condition(threading.Lock())
        self._delivery_generation = 0
        self._prepare_database()
        self._initialize()

    def _clock(self) -> float:
        return _number(self._clock_source(), "managed_control_clock_invalid")

    def _prepare_database(self) -> None:
        parent = self.database.parent
        try:
            if not parent.exists() and not parent.is_symlink():
                parent.mkdir(parents=True, mode=0o700)
                parent.chmod(0o700)
            parent_meta = parent.lstat()
            _require(
                stat.S_ISDIR(parent_meta.st_mode)
                and not stat.S_ISLNK(parent_meta.st_mode)
                and parent_meta.st_uid == os.getuid()
                and stat.S_IMODE(parent_meta.st_mode) == 0o700,
                "managed_control_state_path_invalid",
            )
            if self.database.exists() or self.database.is_symlink():
                metadata = self.database.lstat()
                _require(
                    stat.S_ISREG(metadata.st_mode)
                    and not stat.S_ISLNK(metadata.st_mode)
                    and metadata.st_uid == os.getuid()
                    and metadata.st_nlink == 1
                    and stat.S_IMODE(metadata.st_mode) == 0o600,
                    "managed_control_state_path_invalid",
                )
            else:
                descriptor = os.open(
                    self.database,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    os.fchmod(descriptor, 0o600)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except ManagedControlError:
            raise
        except OSError as exc:
            raise ManagedControlError("managed_control_state_path_invalid") from exc

    def _connect(self) -> sqlite3.Connection:
        _require(not self._closed, "managed_control_closed")
        connection = sqlite3.connect(self.database, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA journal_mode = DELETE")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS managed_metadata (
                    key TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS managed_commands (
                    command_id TEXT PRIMARY KEY NOT NULL,
                    envelope_digest TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    target_node_id TEXT NOT NULL,
                    target_endpoint_id TEXT NOT NULL,
                    target_key_digest TEXT NOT NULL,
                    membership_generation INTEGER NOT NULL,
                    member_incarnation TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    owner_sequence INTEGER NOT NULL,
                    owner_nonce TEXT NOT NULL,
                    command_expires_at REAL NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'pending','claimed','reconciliation_required','completed','failed','expired'
                    )),
                    claim_token TEXT,
                    claimed_request_id TEXT,
                    claimed_at REAL,
                    native_response_digest TEXT,
                    native_response_json TEXT,
                    terminal_receipt_json TEXT,
                    updated_at REAL NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS managed_auth_requests (
                    signer_key_digest TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    nonce TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    PRIMARY KEY (signer_key_digest, request_id),
                    UNIQUE (signer_key_digest, sequence),
                    UNIQUE (signer_key_digest, nonce)
                ) WITHOUT ROWID;
                """
            )
            expected = {
                "schema_version": "1",
                "swarm_id": self.coordinator.swarm_id,
                "owner_node_id": self.coordinator.seed_node_id,
                "owner_endpoint_id": self.owner_signer.endpoint_id,
                "owner_key_digest": self.owner_signer.verification_key_digest,
            }
            rows = connection.execute("SELECT key, value FROM managed_metadata").fetchall()
            existing = {str(row["key"]): str(row["value"]) for row in rows}
            if not existing:
                connection.executemany(
                    "INSERT INTO managed_metadata (key, value) VALUES (?, ?)",
                    tuple(sorted(expected.items())),
                )
            elif existing != expected:
                raise ManagedControlError("managed_control_identity_binding_mismatch")
            connection.commit()
        except ManagedControlError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise ManagedControlError("managed_control_state_unavailable") from exc
        finally:
            connection.close()

    @contextmanager
    def _member_guard(self, statement: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        _validate_binding(statement)
        from .managed_classes import is_managed_peer_class

        try:
            # The guard re-checks the class and generation under its own lock;
            # a class cannot change without a generation change.
            peer_class = self.coordinator.member(statement["target_node_id"])["peer_class"]
            if not is_managed_peer_class(peer_class):
                raise ManagedControlError("managed_control_member_stale")
            with self.coordinator.member_authority_guard(
                node_id=statement["target_node_id"],
                expected_generation=statement["membership_generation"],
                expected_peer_class=peer_class,
                eligible_lifecycle_states=_MEMBER_STATES,
            ) as member:
                if any(
                    member[field] != statement[statement_field]
                    for field, statement_field in (
                        ("node_id", "target_node_id"),
                        ("endpoint_id", "target_endpoint_id"),
                        ("verification_key_digest", "target_key_digest"),
                        ("generation", "membership_generation"),
                        ("incarnation", "member_incarnation"),
                    )
                ):
                    raise ManagedControlError("managed_control_member_stale")
                yield member
        except ManagedControlError:
            raise
        except SeedCoordinatorError as exc:
            raise ManagedControlError("managed_control_member_stale") from exc

    def _verify_owner(
        self, envelope: Mapping[str, Any], *, protocol: str = OWNER_COMMAND_PROTOCOL
    ) -> dict[str, Any]:
        statement = _verify_signed(
            envelope,
            expected_protocol=protocol,
            expected_key_record=self.owner_signer.public_key_record(),
            expected_endpoint_id=self.owner_signer.endpoint_id,
            now=self._clock(),
        )
        _require(set(statement) == _OWNER_FIELDS, "managed_control_fields_invalid")
        _validate_request_common(statement)
        _require(
            statement["swarm_id"] == self.coordinator.swarm_id
            and statement["owner_node_id"] == self.coordinator.seed_node_id,
            "managed_control_authority_binding_invalid",
        )
        command = validate_native_command(statement["native_command"])
        _require(
            command["run_id"] == statement["run_id"]
            and command["deployment_id"] == statement["deployment_id"],
            "managed_control_run_binding_mismatch",
        )
        return statement

    def _verify_target(
        self, envelope: Mapping[str, Any], *, protocol: str, fields: frozenset[str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raw_statement = envelope.get("statement") if isinstance(envelope, Mapping) else None
        _require(isinstance(raw_statement, Mapping), "managed_control_envelope_invalid")
        _require(set(raw_statement) == fields, "managed_control_fields_invalid")
        _validate_request_common(raw_statement)
        _require(
            raw_statement["swarm_id"] == self.coordinator.swarm_id,
            "managed_control_authority_binding_invalid",
        )
        with self._member_guard(raw_statement) as member:
            statement = _verify_signed(
                envelope,
                expected_protocol=protocol,
                expected_key_record={
                    **dict(envelope.get("verification_key", {})),
                },
                expected_endpoint_id=member["endpoint_id"],
                now=self._clock(),
            )
            _require(
                statement["target_key_digest"] == member["verification_key_digest"]
                and envelope["verification_key"].get("verification_key_digest")
                == member["verification_key_digest"],
                "managed_control_key_pin_mismatch",
            )
            return statement, member

    def _response(self, protocol: str, request_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        now = self._clock()
        statement = {
            "protocol": protocol,
            "request_id": request_id,
            "swarm_id": self.coordinator.swarm_id,
            "owner_node_id": self.coordinator.seed_node_id,
            "owner_endpoint_id": self.owner_signer.endpoint_id,
            "issued_at": now,
            "expires_at": now + min(60.0, MAX_TTL_SECONDS),
            **dict(fields),
        }
        return sign_managed_statement(self.owner_signer, statement)

    @staticmethod
    def _cached_or_guard(
        connection: sqlite3.Connection,
        *,
        signer_digest: str,
        statement: Mapping[str, Any],
        request_digest: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT request_digest, response_json FROM managed_auth_requests "
            "WHERE signer_key_digest = ? AND request_id = ?",
            (signer_digest, statement["request_id"]),
        ).fetchone()
        if row is not None:
            _require(
                row["request_digest"] == request_digest,
                "managed_control_conflicting_retry",
            )
            return json.loads(row["response_json"])
        nonce = connection.execute(
            "SELECT 1 FROM managed_auth_requests WHERE signer_key_digest = ? AND nonce = ?",
            (signer_digest, statement["nonce"]),
        ).fetchone()
        _require(nonce is None, "managed_control_nonce_replayed")
        maximum = connection.execute(
            "SELECT MAX(sequence) AS maximum FROM managed_auth_requests "
            "WHERE signer_key_digest = ?",
            (signer_digest,),
        ).fetchone()["maximum"]
        _require(
            maximum is None or statement["sequence"] > maximum,
            "managed_control_sequence_replayed",
        )
        return None

    @staticmethod
    def _record_auth(
        connection: sqlite3.Connection,
        *,
        signer_digest: str,
        statement: Mapping[str, Any],
        request_digest: str,
        response: Mapping[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO managed_auth_requests "
            "(signer_key_digest, request_id, request_digest, sequence, nonce, protocol, response_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                signer_digest,
                statement["request_id"],
                request_digest,
                statement["sequence"],
                statement["nonce"],
                statement["protocol"],
                canonical_json_bytes(dict(response)).decode("utf-8"),
            ),
        )

    def enqueue(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Authenticate and atomically enqueue one owner command."""

        with self._lock:
            statement = self._verify_owner(envelope)
            command = statement["native_command"]
            request_digest = _document_digest(envelope)
            signer_digest = self.owner_signer.verification_key_digest
            with self._member_guard(statement):
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    cached = self._cached_or_guard(
                        connection,
                        signer_digest=signer_digest,
                        statement=statement,
                        request_digest=request_digest,
                    )
                    if cached is not None:
                        connection.commit()
                        return cached
                    prior = connection.execute(
                        "SELECT envelope_digest FROM managed_commands WHERE command_id = ?",
                        (command["command_id"],),
                    ).fetchone()
                    if prior is not None:
                        raise ManagedControlError("managed_control_conflicting_retry")
                    outstanding = connection.execute(
                        "SELECT COUNT(*) FROM managed_commands WHERE target_node_id = ? "
                        "AND run_id = ? AND deployment_id = ? AND "
                        "(state IN ('pending','claimed') OR "
                        "(state = 'reconciliation_required' AND terminal_receipt_json IS NULL))",
                        (statement["target_node_id"], statement["run_id"], statement["deployment_id"]),
                    ).fetchone()[0]
                    _require(outstanding < _MAX_PENDING_COMMANDS, "managed_control_capacity_exhausted")
                    now = self._clock()
                    connection.execute(
                        "INSERT INTO managed_commands "
                        "(command_id, envelope_digest, envelope_json, target_node_id, "
                        "target_endpoint_id, target_key_digest, membership_generation, "
                        "member_incarnation, run_id, deployment_id, owner_sequence, owner_nonce, "
                        "command_expires_at, state, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (
                            command["command_id"],
                            request_digest,
                            canonical_json_bytes(dict(envelope)).decode("utf-8"),
                            statement["target_node_id"],
                            statement["target_endpoint_id"],
                            statement["target_key_digest"],
                            statement["membership_generation"],
                            statement["member_incarnation"],
                            statement["run_id"],
                            statement["deployment_id"],
                            statement["sequence"],
                            statement["nonce"],
                            statement["expires_at"],
                            now,
                        ),
                    )
                    response = self._response(
                        ENQUEUE_RECEIPT_PROTOCOL,
                        statement["request_id"],
                        {
                            **{field: statement[field] for field in _BINDING_FIELDS},
                            "command_id": command["command_id"],
                            "command_digest": _document_digest(command),
                            "state": "pending",
                        },
                    )
                    self._record_auth(
                        connection,
                        signer_digest=signer_digest,
                        statement=statement,
                        request_digest=request_digest,
                        response=response,
                    )
                    connection.commit()
                    # The command is durable now, so a parked poll for this member may
                    # claim it.  Signalled AFTER the commit: a woken poll re-attempts
                    # against committed state, never against a half-written row.
                    self._signal_delivery()
                    return response
                except ManagedControlError:
                    connection.rollback()
                    raise
                except (sqlite3.Error, TypeError, ValueError) as exc:
                    connection.rollback()
                    raise ManagedControlError("managed_control_state_unavailable") from exc
                finally:
                    connection.close()

    def _expire_pending(
        self,
        connection: sqlite3.Connection,
        *,
        statement: Mapping[str, Any],
        binding_values: tuple[Any, ...],
    ) -> None:
        """Terminally fence stale owner commands before any target delivery."""

        rows = connection.execute(
            "SELECT command_id, envelope_json FROM managed_commands "
            "WHERE target_node_id = ? AND target_endpoint_id = ? "
            "AND target_key_digest = ? AND membership_generation = ? "
            "AND member_incarnation = ? AND run_id = ? AND deployment_id = ? "
            "AND state = 'pending' AND command_expires_at < ? "
            "ORDER BY owner_sequence",
            (*binding_values, self._clock()),
        ).fetchall()
        for row in rows:
            try:
                owner_envelope = json.loads(row["envelope_json"])
                owner_statement = owner_envelope["statement"]
                request_id = owner_statement["request_id"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ManagedControlError("managed_control_state_corrupt") from exc
            receipt = self._response(
                ACK_RECEIPT_PROTOCOL,
                request_id,
                {
                    **{field: statement[field] for field in _BINDING_FIELDS},
                    "command_id": row["command_id"],
                    "state": "expired",
                    "native_response_digest": None,
                },
            )
            connection.execute(
                "UPDATE managed_commands SET state = 'expired', "
                "terminal_receipt_json = ?, updated_at = ? "
                "WHERE command_id = ? AND state = 'pending'",
                (
                    canonical_json_bytes(receipt).decode("utf-8"),
                    self._clock(),
                    row["command_id"],
                ),
            )

    @staticmethod
    def _is_concurrent_poll(envelope: Mapping[str, Any]) -> bool:
        """True for the concurrent (v2) poll the target's `ConcurrentTargetControlPump` issues."""

        statement = envelope.get("statement") if isinstance(envelope, Mapping) else None
        return (
            isinstance(statement, Mapping)
            and statement.get("protocol") == TARGET_POLL_PROTOCOL_V2
        )

    def _outstanding_claim_deadline_is_live(
        self, active_claims: Mapping[str, Any]
    ) -> bool:
        """True while any claim the peer owes still has a live owner deadline.

        ``active_claims`` lists every claim the target pump has not terminally
        acked.  Each claim's own ``command_expires_at`` is the deadline the
        owner's waiter uses (``control_session._send``:
        ``ttl_seconds=min(60.0, _remaining(deadline))``), so one deadline per
        outstanding claim is enough to decide whether parking the poll could
        delay an acknowledgement somebody is still observing.

        Fails closed: a claim this mailbox cannot resolve - a command id it does
        not hold, or a row without a usable expiry - counts as live, so an
        unknown claim can never license a hold.
        """

        command_ids = tuple(sorted(active_claims))
        if not command_ids:
            return False
        placeholders = ", ".join("?" for _ in command_ids)
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT command_id, command_expires_at FROM managed_commands "
                    f"WHERE command_id IN ({placeholders})",
                    command_ids,
                ).fetchall()
            finally:
                connection.close()
        observed = {
            row["command_id"]: row["command_expires_at"] for row in rows
        }
        now = self._clock()
        for command_id in command_ids:
            expires_at = observed.get(command_id)
            if not isinstance(expires_at, (int, float)) or isinstance(
                expires_at, bool
            ):
                return True
            if float(expires_at) >= now:
                return True
        return False

    def _signal_delivery(self) -> None:
        """Wake every parked poll: a command has just become deliverable.

        Called from ``enqueue`` after the row is committed, so a woken poll re-attempts
        against durable state and can never observe a command that is not yet there.
        Never raises and never blocks: a signal is not an outcome.
        """

        with self._delivery_wakeup:
            self._delivery_generation += 1
            self._delivery_wakeup.notify_all()

    def _delivery_generation_now(self) -> int:
        """The signal generation to measure the NEXT attempt's wait against.

        Read *before* an attempt, so a signal that lands while that attempt is in
        flight is still visible to the wait that follows it (``_wait_for_delivery``).
        """

        with self._delivery_wakeup:
            return self._delivery_generation

    def _wait_for_delivery(self, timeout: float, *, observed: int | None = None) -> bool:
        """Wait up to ``timeout`` for a delivery signal; True if one arrived.

        ``timeout`` is the caller's own step -- unchanged, and still the ceiling, so a
        missed signal can never park a peer for longer than the interval it used
        before.  A signal only ever makes the same wait *shorter*.

        ``observed`` is the generation the caller read **before** its attempt.  If the
        generation has already moved on, an enqueue committed *during* that attempt: the
        command is deliverable now, so this returns immediately instead of spending the
        interval rediscovering it.  Without that, a signal that lands in the window
        between an attempt returning empty and this wait starting is a lost wake-up and
        the claim latency for that command is a full ``long_poll_step`` -- measured 1.03
        to 1.06 s at a 1.0 s step, and the same shape the parked-poll wake-up exists to
        remove (p81r1's ``infer_cancel_wait`` claimed at 3 957 ms of a 4 427 ms life).
        """

        deadline = time.monotonic() + float(timeout)
        with self._delivery_wakeup:
            generation = self._delivery_generation if observed is None else observed
            while self._delivery_generation == generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._delivery_wakeup.wait(remaining)
            return True

    def _may_hold(self, envelope: Mapping[str, Any]) -> bool:
        """True only for a concurrent poll no *live* deadline is waiting on.

        ``active_claims`` lists every claim the peer has not terminally acked, so a
        non-empty map means this peer still has native work to report and a parked
        poll would delay exactly that acknowledgement -- the pump performs one
        HTTP operation per ``step()``, so it cannot ack while the hold is in
        flight.  That is not theoretical: an ungated 1.5 s hold failed
        ``tests/a13_macos/test_owner_concurrency.py`` (a 1.0 s owner deadline
        against a 1.5 s park), and the mailbox ledger shows a health claim was
        still outstanding when the cleanup cancel arrived, so the peer is usually
        *not* idle when a command lands.

        That second measurement is why the gate is now expressed as a *deadline*
        rule rather than "the map is empty" (wp5-cleanup-levers).  Each claim's own
        ``command_expires_at`` is the deadline its owner waiter uses
        (``control_session._send`` bounds the enqueue TTL by the waiter's own
        remaining time), so:

        * a claim with a **live** deadline - the case the ungated hold broke -
          refuses the hold outright: the park would delay an acknowledgement
          somebody is still waiting for, and no bound derived from that deadline
          can undo the delay, because the ack itself needs one pump ``step()``
          plus its return transit *after* the park ends;
        * a claim whose deadline has **already passed** cannot be observed by any
          live waiter, so holding that poll delays nothing anyone is waiting on -
          and it is exactly the measured round-50/52 shape (a health probe the
          route had already stopped waiting for when the cancel landed), which is
          the claim latency this hold exists to remove.

        A peer that owes nothing is unchanged, and unknown claims fail closed in
        ``_outstanding_claim_deadline_is_live``.
        """

        if not self._is_concurrent_poll(envelope):
            return False
        active = envelope["statement"].get("active_claims")
        if not isinstance(active, Mapping):
            return False
        if not active:
            return True
        return not self._outstanding_claim_deadline_is_live(active)

    def poll(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Authenticate a target poll and claim at most one command atomically.

        A concurrent poll that finds nothing to deliver **and** reports no
        outstanding claim is held open for a bounded period instead of returning
        the empty receipt immediately: the peer's next poll is otherwise a whole
        relay round trip away, and that round trip is exactly the claim latency
        that kept a relayed peer's cleanup from being provable inside the route's
        immutable cancellation window (measured 1.030 s relayed against 0.119 s on
        the same machine, with the identical 50 ms pump cadence on both).

        The wait sits BETWEEN attempts, never inside one.  Each attempt takes the
        process-wide mailbox lock, the member-authority guard and one
        ``BEGIN IMMEDIATE`` transaction and releases all three before the step
        sleep, so the owner's own ``enqueue`` is never serialised behind a held
        poll -- which matters because the enqueue this exists to deliver quickly
        is the cancellation itself.

        Two narrower scopes, both deliberate (see `_may_hold`): a peer that still
        owes an acknowledgement whose deadline is *live* is never parked, and a
        legacy serial poll keeps its immediate empty receipt.

        The wait BETWEEN attempts is a delivery **wake-up**, not the interval: an
        ``enqueue`` signals every parked poll, so a poll that is already open returns the
        new command at once instead of at its next interval tick (`_signal_delivery`).
        ``long_poll_step`` is unchanged and still the ceiling on the same wait, so a
        missed signal can never park a peer for longer than before; only the promptness
        of the delivery path changes.

        The signal generation is read *before* each attempt and handed to the wait that
        follows it, so an ``enqueue`` that commits while an attempt is in flight is not a
        lost wake-up either: that command is already deliverable and the next attempt
        starts at once.  Reading it after the attempt left exactly that window, and a
        poll that fell into it paid a whole ``long_poll_step`` for a command that was
        already waiting.
        """

        if self.long_poll_hold <= 0.0 or not self._may_hold(envelope):
            return self._poll_once(envelope)
        deadline = None
        while True:
            observed = self._delivery_generation_now()
            response = self._poll_once(envelope)
            if not _empty_poll_delivery(response):
                return response
            now = self._monotonic()
            if deadline is None:
                deadline = now + self.long_poll_hold
            remaining = deadline - now
            if remaining <= 0.0:
                return response
            self._wait_for_delivery(min(self.long_poll_step, remaining), observed=observed)

    def _poll_once(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """One authoritative claim attempt: verify, lock, one transaction, release."""

        with self._lock:
            concurrent = self._is_concurrent_poll(envelope)
            statement, member = self._verify_target(
                envelope, protocol=TARGET_POLL_PROTOCOL_V2 if concurrent else TARGET_POLL_PROTOCOL,
                fields=_POLL_V2_FIELDS if concurrent else _POLL_FIELDS,
            )
            active = statement.get("active_claims", {})
            _require(isinstance(active, dict) and len(active) <= _MAX_ACTIVE_WORK + _MAX_ACTIVE_CONTROL,
                "managed_control_active_claim_invalid")
            for command_id, claim_token in active.items():
                _segment(command_id, "managed_control_active_claim_invalid")
                _segment(claim_token, "managed_control_active_claim_invalid")
            request_digest = _document_digest(envelope)
            signer_digest = member["verification_key_digest"]
            # Re-enter the authority guard at the delivery transaction boundary.
            with self._member_guard(statement):
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    cached = self._cached_or_guard(
                        connection,
                        signer_digest=signer_digest,
                        statement=statement,
                        request_digest=request_digest,
                    )
                    # An empty poll receipt is NOT a final outcome.  Every
                    # re-attempt inside the bounded hold replays this very signed
                    # request, so returning the recorded empty reply here would
                    # make the whole hold a silent no-op.  The row itself is kept
                    # -- one request id, one nonce, one sequence, exactly as
                    # before -- so replay protection is unchanged; only its
                    # recorded reply is replaced by the later delivery.
                    replacing_empty = cached is not None and _empty_poll_delivery(cached)
                    if cached is not None and not replacing_empty:
                        connection.commit()
                        return cached
                    binding_values = tuple(statement[field] for field in (
                        "target_node_id", "target_endpoint_id", "target_key_digest",
                        "membership_generation", "member_incarnation", "run_id", "deployment_id",
                    ))
                    self._expire_pending(
                        connection,
                        statement=statement,
                        binding_values=binding_values,
                    )
                    claimed = connection.execute(
                        "SELECT * FROM managed_commands WHERE target_node_id = ? "
                        "AND target_endpoint_id = ? AND target_key_digest = ? "
                        "AND membership_generation = ? AND member_incarnation = ? "
                        "AND run_id = ? AND deployment_id = ? "
                        "AND (state = 'claimed' OR "
                        "(state = 'reconciliation_required' AND terminal_receipt_json IS NULL)) "
                        "ORDER BY owner_sequence",
                        binding_values,
                    ).fetchall()
                    by_id = {row["command_id"]: row for row in claimed}
                    for command_id, claim_token in active.items():
                        _require(command_id in by_id and by_id[command_id]["claim_token"] == claim_token,
                            "managed_control_active_claim_invalid")
                    unresolved = [row for row in claimed
                        if row["state"] == "reconciliation_required" or row["command_id"] not in active]
                    delivery_kind = "empty"
                    command_id = None
                    claim_token = None
                    command_envelope = None
                    if unresolved:
                        row = unresolved[0]
                        command_id = row["command_id"]
                        claim_token = row["claim_token"]
                        delivery_kind = "reconciliation_required"
                        if row["state"] == "claimed":
                            connection.execute(
                                "UPDATE managed_commands SET state = 'reconciliation_required', "
                                "updated_at = ? WHERE command_id = ? AND state = 'claimed'",
                                (self._clock(), command_id),
                            )
                    else:
                        def native(row):
                            return json.loads(row["envelope_json"])["statement"]["native_command"]
                        work_count = sum(native(row)["command"] in _WORK_OPERATIONS for row in claimed)
                        control_count = len(claimed) - work_count
                        _require(work_count <= _MAX_ACTIVE_WORK and control_count <= _MAX_ACTIVE_CONTROL,
                            "managed_control_active_claim_invalid")
                        pending = connection.execute(
                            "SELECT * FROM managed_commands WHERE target_node_id = ? "
                            "AND target_endpoint_id = ? AND target_key_digest = ? "
                            "AND membership_generation = ? AND member_incarnation = ? "
                            "AND run_id = ? AND deployment_id = ? AND state = 'pending' "
                            "ORDER BY owner_sequence LIMIT ?",
                            (*binding_values, _MAX_PENDING_COMMANDS + 1),
                        ).fetchall()
                        _require(len(pending) <= _MAX_PENDING_COMMANDS, "managed_control_capacity_exhausted")
                        if concurrent:
                            def priority(row):
                                command = native(row)
                                cleanup = command["command"] in {"infer_cancel_wait", "infer_cancel", "cancel", "stop"}
                                cleanup |= command["command"] == "snapshot" and "cleanup_subject" in command["payload"]
                                return (0 if cleanup else (2 if command["command"] in _WORK_OPERATIONS else 1), row["owner_sequence"])
                            pending.sort(key=priority)
                        for row in pending:
                            work = native(row)["command"] in _WORK_OPERATIONS
                            if concurrent and ((work and work_count >= _MAX_ACTIVE_WORK)
                                    or (not work and control_count >= _MAX_ACTIVE_CONTROL)):
                                continue
                            claim_token = _segment(self._claim_token_source(), "managed_control_claim_token_invalid")
                            delivery_kind = "command"
                            command_id = row["command_id"]
                            command_envelope = json.loads(row["envelope_json"])
                            connection.execute(
                                "UPDATE managed_commands SET state = 'claimed', claim_token = ?, "
                                "claimed_request_id = ?, claimed_at = ?, updated_at = ? "
                                "WHERE command_id = ? AND state = 'pending'",
                                (claim_token, statement["request_id"], self._clock(), self._clock(), command_id),
                            )
                            break
                    response = self._response(
                        POLL_RECEIPT_PROTOCOL,
                        statement["request_id"],
                        {
                            **{field: statement[field] for field in _BINDING_FIELDS},
                            "delivery_kind": delivery_kind,
                            "command_id": command_id,
                            "claim_token": claim_token,
                            "command_envelope": command_envelope,
                        },
                    )
                    if replacing_empty:
                        self._replace_auth_response(
                            connection,
                            signer_digest=signer_digest,
                            statement=statement,
                            request_digest=request_digest,
                            response=response,
                        )
                    else:
                        self._record_auth(
                            connection,
                            signer_digest=signer_digest,
                            statement=statement,
                            request_digest=request_digest,
                            response=response,
                        )
                    connection.commit()
                    return response
                except ManagedControlError:
                    connection.rollback()
                    raise
                except (sqlite3.Error, TypeError, ValueError) as exc:
                    connection.rollback()
                    raise ManagedControlError("managed_control_state_unavailable") from exc
                finally:
                    connection.close()

    @staticmethod
    def _replace_auth_response(
        connection: sqlite3.Connection,
        *,
        signer_digest: str,
        statement: Mapping[str, Any],
        request_digest: str,
        response: Mapping[str, Any],
    ) -> None:
        """Record a later delivery for an already-authenticated EMPTY poll reply.

        Used only by the bounded long poll, and only when the recorded reply for
        this exact request id was an empty delivery.  The row is updated in
        place: request id, nonce and sequence are untouched, so the single-use
        replay guarantees of ``managed_auth_requests`` hold exactly as before --
        one row per signed request, one nonce, one sequence.  A delivered reply
        is never overwritten, so a lost-response retry still replays its claim
        verbatim.
        """

        updated = connection.execute(
            "UPDATE managed_auth_requests SET response_json = ? "
            "WHERE signer_key_digest = ? AND request_id = ? AND request_digest = ?",
            (
                canonical_json_bytes(dict(response)).decode("utf-8"),
                signer_digest,
                statement["request_id"],
                request_digest,
            ),
        ).rowcount
        _require(updated == 1, "managed_control_state_unavailable")

    def ack(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Persist an authenticated terminal receipt or unresolved reconciliation."""

        with self._lock:
            statement, member = self._verify_target(
                envelope, protocol=TARGET_ACK_PROTOCOL, fields=_diagnostic_fields(envelope, _ACK_FIELDS)
            )
            diagnostic = _target_diagnostic(statement)
            _segment(statement["command_id"])
            _segment(statement["claim_token"], "managed_control_claim_token_invalid")
            _require(
                statement["disposition"]
                in {"completed", "failed", "reconciliation_required"},
                "managed_control_disposition_invalid",
            )
            native_response = None
            native_digest = None
            if statement["disposition"] == "reconciliation_required":
                _require(
                    statement["native_response"] is None,
                    "managed_control_native_response_invalid",
                )
            else:
                native_response = _validate_native_response(
                    statement["native_response"],
                    command_id=statement["command_id"],
                    node_id=statement["target_node_id"],
                    disposition=statement["disposition"],
                )
                native_digest = _document_digest(native_response)
            request_digest = _document_digest(envelope)
            signer_digest = member["verification_key_digest"]
            with self._member_guard(statement):
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    cached = self._cached_or_guard(
                        connection,
                        signer_digest=signer_digest,
                        statement=statement,
                        request_digest=request_digest,
                    )
                    if cached is not None:
                        connection.commit()
                        return cached
                    row = connection.execute(
                        "SELECT * FROM managed_commands WHERE command_id = ?",
                        (statement["command_id"],),
                    ).fetchone()
                    _require(row is not None, "managed_control_command_unknown")
                    if diagnostic:
                        original = json.loads(row['envelope_json'])['statement']['native_command']
                        _require(original['command'] == 'install_token_path',
                                 'managed_control_target_diagnostic_invalid')
                    for column, field in (
                        ("target_node_id", "target_node_id"),
                        ("target_endpoint_id", "target_endpoint_id"),
                        ("target_key_digest", "target_key_digest"),
                        ("membership_generation", "membership_generation"),
                        ("member_incarnation", "member_incarnation"),
                        ("run_id", "run_id"),
                        ("deployment_id", "deployment_id"),
                    ):
                        _require(
                            row[column] == statement[field],
                            "managed_control_command_binding_mismatch",
                        )
                    _require(
                        row["claim_token"] == statement["claim_token"],
                        "managed_control_claim_token_mismatch",
                    )
                    disposition = statement["disposition"]
                    reconciled_unknown = (
                        row["state"] == "reconciliation_required"
                        and row["terminal_receipt_json"] is not None
                    )
                    if row["state"] in _TERMINAL_STATES or reconciled_unknown:
                        retained = {} if row['terminal_receipt_json'] is None else json.loads(row['terminal_receipt_json'])['statement']
                        _require(_target_diagnostic(retained) == diagnostic,
                                 'managed_control_conflicting_retry')
                        _require(
                            row["state"] == disposition
                            and row["native_response_digest"] == native_digest,
                            "managed_control_conflicting_retry",
                        )
                    elif disposition == "reconciliation_required":
                        _require(
                            row["state"] in {"claimed", "reconciliation_required"}
                            and row["terminal_receipt_json"] is None,
                            "managed_control_ack_state_invalid",
                        )
                        # The peer has durably stated that the native outcome is
                        # unknown.  Preserve that exact state for owner result
                        # inspection, but make it terminal with a receipt so later
                        # polls do not re-demand the same reconciliation forever.
                        connection.execute(
                            "UPDATE managed_commands SET state = 'reconciliation_required', "
                            "native_response_digest = NULL, native_response_json = NULL, "
                            "updated_at = ? WHERE command_id = ?",
                            (self._clock(), statement["command_id"]),
                        )
                    else:
                        _require(
                            row["state"] in {"claimed", "reconciliation_required"}
                            and row["terminal_receipt_json"] is None,
                            "managed_control_ack_state_invalid",
                        )
                        connection.execute(
                            "UPDATE managed_commands SET state = ?, native_response_digest = ?, "
                            "native_response_json = ?, updated_at = ? WHERE command_id = ?",
                            (
                                disposition,
                                native_digest,
                                canonical_json_bytes(native_response).decode("utf-8"),
                                self._clock(),
                                statement["command_id"],
                            ),
                        )
                    response = self._response(
                        ACK_RECEIPT_PROTOCOL,
                        statement["request_id"],
                        {
                            **{field: statement[field] for field in _BINDING_FIELDS},
                            "command_id": statement["command_id"],
                            "state": disposition,
                            "native_response_digest": native_digest,
                            **diagnostic,
                        },
                    )
                    if disposition in _TERMINAL_STATES or disposition == "reconciliation_required":
                        connection.execute(
                            "UPDATE managed_commands SET terminal_receipt_json = ? "
                            "WHERE command_id = ?",
                            (
                                canonical_json_bytes(response).decode("utf-8"),
                                statement["command_id"],
                            ),
                        )
                    self._record_auth(
                        connection,
                        signer_digest=signer_digest,
                        statement=statement,
                        request_digest=request_digest,
                        response=response,
                    )
                    connection.commit()
                    return response
                except ManagedControlError:
                    connection.rollback()
                    raise
                except (sqlite3.Error, TypeError, ValueError) as exc:
                    connection.rollback()
                    raise ManagedControlError("managed_control_state_unavailable") from exc
                finally:
                    connection.close()

    def result(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Owner-authenticated fresh observation of an exact command's output.

        Does not deliver or re-execute anything. Current-member fencing applies
        even to cached reads; fresh request IDs are needed for fresh snapshots.
        No target claim capability or credential is returned.
        """
        with self._lock:
            statement = self._verify_owner(envelope, protocol=OWNER_RESULT_PROTOCOL)
            command = statement["native_command"]
            signer_digest = self.owner_signer.verification_key_digest
            request_digest = _document_digest(envelope)
            with self._member_guard(statement):
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    cached = self._cached_or_guard(connection,
                        signer_digest=signer_digest, statement=statement,
                        request_digest=request_digest)
                    if cached is not None:
                        connection.commit()
                        return cached
                    binding_values = tuple(statement[field] for field in (
                        "target_node_id", "target_endpoint_id", "target_key_digest",
                        "membership_generation", "member_incarnation", "run_id", "deployment_id"))
                    self._expire_pending(connection, statement=statement, binding_values=binding_values)
                    row = connection.execute("SELECT * FROM managed_commands WHERE command_id = ?",
                        (command["command_id"],)).fetchone()
                    _require(row is not None, "managed_control_command_unknown")
                    original = json.loads(row["envelope_json"])["statement"]["native_command"]
                    _require(all(row[field] == statement[field] for field in _BINDING_FIELDS)
                        and original == command, "managed_control_command_binding_mismatch")
                    native = None if row["native_response_json"] is None else json.loads(row["native_response_json"])
                    if row["state"] in {"completed", "failed"}:
                        _validate_native_response(native, command_id=command["command_id"],
                            node_id=statement["target_node_id"], disposition=row["state"])
                        _require(_document_digest(native) == row["native_response_digest"],
                            "managed_control_state_corrupt")
                    else:
                        _require(native is None and row["native_response_digest"] is None,
                            "managed_control_state_corrupt")
                    terminal = {} if row['terminal_receipt_json'] is None else json.loads(row['terminal_receipt_json'])['statement']
                    diagnostic = _target_diagnostic(terminal)
                    response = self._response(RESULT_RECEIPT_PROTOCOL, statement["request_id"], {
                        **{field: statement[field] for field in _BINDING_FIELDS},
                        "command_id": command["command_id"], "command_digest": _document_digest(command),
                        "state": row["state"], "native_response": native,
                        "native_response_digest": row["native_response_digest"], **diagnostic})
                    self._record_auth(connection, signer_digest=signer_digest, statement=statement,
                        request_digest=request_digest, response=response)
                    connection.commit()
                    return response
                except ManagedControlError:
                    connection.rollback()
                    raise
                except (sqlite3.Error, TypeError, ValueError, KeyError) as exc:
                    connection.rollback()
                    raise ManagedControlError("managed_control_state_unavailable") from exc
                finally:
                    connection.close()

    def command_receipt(self, command_id: str) -> dict[str, Any]:
        """Return an owner-local status projection; this is not an HTTP route."""

        command_id = _segment(command_id)
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT command_id, state, target_node_id, membership_generation, "
                    "member_incarnation, run_id, deployment_id, envelope_digest, "
                    "native_response_digest FROM managed_commands WHERE command_id = ?",
                    (command_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise ManagedControlError("managed_control_state_unavailable") from exc
            finally:
                connection.close()
        _require(row is not None, "managed_control_command_unknown")
        return dict(row)

    def handle_http(
        self, method: str, path: str, body: Mapping[str, Any]
    ) -> tuple[int, Mapping[str, Any]]:
        """Minimal mountable canonical-JSON API for an existing HTTPS server."""

        _require(method == "POST", "managed_control_method_invalid")
        routes = {
            "/managed-control/enqueue": self.enqueue,
            "/managed-control/poll": self.poll,
            "/managed-control/ack": self.ack,
            "/managed-control/result": self.result,
            "/managed-control/token-progress": self.token_progress,
        }
        handler = routes.get(path)
        _require(handler is not None, "managed_control_route_unknown")
        return HTTPStatus.OK, handler(body)

    def close(self) -> None:
        self._closed = True
        self._close_token_progress()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class ManagedControlClient:
    """Web-PKI HTTPS, no-redirect client for owner enqueue and target poll/ack."""

    def __init__(
        self,
        *,
        base_url: str,
        signer: Ed25519EvidenceSigner,
        trusted_owner_key_record: Mapping[str, Any],
        swarm_id: str,
        owner_node_id: str,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
        max_prepared_requests: int = 256,
        trusted_owner_endpoint_id: str | None = None,
        _local_tls_ca_certificate: Path | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        _require(
            parts.scheme == "https"
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and parts.path in {"", "/"}
            and not parts.query
            and not parts.fragment,
            "managed_control_origin_invalid",
        )
        _require(isinstance(signer, Ed25519EvidenceSigner), "managed_control_signer_invalid")
        _segment(swarm_id)
        _segment(owner_node_id)
        _require(callable(clock), "managed_control_clock_invalid")
        _require(
            not isinstance(timeout, bool)
            and isinstance(timeout, (int, float))
            and math.isfinite(float(timeout))
            and timeout > 0,
            "managed_control_timeout_invalid",
        )
        _require(
            type(max_prepared_requests) is int
            and 2 <= max_prepared_requests <= 10000,
            "managed_control_prepared_capacity_invalid",
        )
        try:
            if _local_tls_ca_certificate is None:
                context = ssl.create_default_context()
            else:
                # Explicit local-installation test seam; production never injects a CA.
                context = ssl.create_default_context(cafile=str(_local_tls_ca_certificate))
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            verifier = build_ed25519_verifier([dict(trusted_owner_key_record)])
            del verifier
        except Exception as exc:
            raise ManagedControlError("managed_control_tls_or_owner_pin_invalid") from exc
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.trusted_owner_key_record = dict(trusted_owner_key_record)
        _require(trusted_owner_endpoint_id is None or (isinstance(trusted_owner_endpoint_id, str)
                 and 0 < len(trusted_owner_endpoint_id) <= 256), "managed_control_owner_pin_invalid")
        self.trusted_owner_endpoint_id = trusted_owner_endpoint_id
        self.swarm_id = swarm_id
        self.owner_node_id = owner_node_id
        self.timeout = float(timeout)
        self._clock_source = clock
        if _local_tls_ca_certificate is None:
            from .public_https import public_opener
            self._opener = public_opener(context=context)
        else:
            self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))
        # Retain exact signed bytes for same-process transport retries.  A
        # restarted caller must persist/reuse its envelope rather than minting
        # a semantically similar request with a new freshness window.  The
        # cache is bounded: the oldest entry is forgotten at capacity and
        # callers resolve entries explicitly once terminally acknowledged.
        self._prepared_requests: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.max_prepared_requests = max_prepared_requests
        self.redirects_allowed = False

    @classmethod
    def for_local_tls(
        cls,
        *,
        ca_certificate: Path,
        **kwargs: Any,
    ) -> "ManagedControlClient":
        """Explicit local TLS test seam using one installation-local CA."""

        return cls(_local_tls_ca_certificate=ca_certificate, **kwargs)

    def _clock(self) -> float:
        return _number(self._clock_source(), "managed_control_clock_invalid")

    def _times(
        self,
        *,
        issued_at: float | None,
        expires_at: float | None,
        ttl_seconds: float,
    ) -> tuple[float, float]:
        issued = self._clock() if issued_at is None else _number(
            issued_at, "managed_control_time_invalid"
        )
        expires = issued + ttl_seconds if expires_at is None else _number(
            expires_at, "managed_control_time_invalid"
        )
        return issued, expires

    def owner_command_statement(
        self,
        *,
        target: Mapping[str, Any],
        native_command: Mapping[str, Any],
        sequence: int,
        nonce: str,
        request_id: str,
        issued_at: float | None = None,
        expires_at: float | None = None,
        ttl_seconds: float = 60.0,
    ) -> dict[str, Any]:
        command = validate_native_command(native_command)
        issued, expires = self._times(
            issued_at=issued_at, expires_at=expires_at, ttl_seconds=ttl_seconds
        )
        statement = {
            "protocol": OWNER_COMMAND_PROTOCOL,
            "request_id": request_id,
            "swarm_id": self.swarm_id,
            "owner_node_id": self.owner_node_id,
            **_binding_from_member(
                target, command["run_id"], command["deployment_id"]
            ),
            "sequence": sequence,
            "nonce": nonce,
            "issued_at": issued,
            "expires_at": expires,
            "native_command": command,
        }
        _validate_request_common(statement)
        _validate_time(statement, now=self._clock())
        return statement

    def _target_statement(
        self,
        *,
        protocol: str,
        target: Mapping[str, Any],
        run_id: str,
        deployment_id: str,
        sequence: int,
        nonce: str,
        request_id: str,
        ttl_seconds: float,
        additions: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        issued = self._clock()
        statement = {
            "protocol": protocol,
            "request_id": request_id,
            "swarm_id": self.swarm_id,
            **_binding_from_member(target, run_id, deployment_id),
            "sequence": sequence,
            "nonce": nonce,
            "issued_at": issued,
            "expires_at": issued + ttl_seconds,
            **({} if additions is None else dict(additions)),
        }
        return statement

    def _signed_outbound(self, statement: Mapping[str, Any]) -> dict[str, Any]:
        """Reuse exact bytes for an identical same-process request retry."""

        request_id = _segment(statement.get("request_id"))
        intent = {
            key: value
            for key, value in statement.items()
            if key not in {"issued_at", "expires_at"}
        }
        cached = self._prepared_requests.get(request_id)
        if cached is not None and cached[0] == intent:
            # A matching retry keeps its exact signed bytes (and freshness
            # window); it only moves to most-recent for capacity accounting.
            del self._prepared_requests[request_id]
            self._prepared_requests[request_id] = (cached[0], cached[1])
            return cached[1]
        if cached is not None:
            # Same request id with a different intent is a caller bug that must
            # fail closed, not an opportunity to mint new signed bytes.
            raise ManagedControlError("managed_control_conflicting_retry")
        envelope = sign_managed_statement(self.signer, statement)
        self._remember_prepared(request_id, _detached(intent), _detached(envelope))
        return envelope

    def _remember_prepared(
        self, request_id: str, intent: dict[str, Any], envelope: dict[str, Any]
    ) -> None:
        """Insert one entry, forgetting the oldest when capacity is reached.

        Forgetting only drops the local byte-reuse guarantee for a request the
        durable caller has already persisted.  It never re-signs, never
        refreshes a signed freshness window, and never reissues a sequence or
        nonce.  A direct duplicate of a forgotten request fails closed at the
        server's request-id/digest conflict guard instead of being replayed.
        """

        self._prepared_requests.pop(request_id, None)
        while len(self._prepared_requests) >= self.max_prepared_requests:
            self._prepared_requests.pop(next(iter(self._prepared_requests)))
        self._prepared_requests[request_id] = (intent, envelope)

    def forget_prepared_request(self, request_id: str) -> None:
        """Explicitly forget one cached signed request (terminal resolution).

        Callers that durably persisted an envelope may call this once its
        outcome is terminally resolved (confirmed enqueue, terminal ack) to
        release the entry before capacity would evict it.
        """

        _segment(request_id)
        self._prepared_requests.pop(request_id, None)

    def prepared_request_ids(self) -> tuple[str, ...]:
        """Oldest-to-newest cached request ids, oldest first."""

        return tuple(self._prepared_requests)

    def _transport_timeout(self):
        interruption = _REQUEST_INTERRUPTION.get()
        _require(interruption is None or not interruption.is_set(), 'managed_control_command_interrupted')
        deadline = _REQUEST_DEADLINE.get()
        if deadline is None:
            return self.timeout
        remaining = deadline - time.monotonic()
        _require(remaining > 0, "managed_control_response_timeout")
        return min(self.timeout, remaining)

    def _read_response(self, response):
        if _REQUEST_DEADLINE.get() is None:
            return response.read(MAX_FRAME_BYTES + 1)
        # Read at most one buffered/socket operation each iteration. A peer that
        # trickles bytes cannot reset the owner's total response-body budget.
        stream = response
        sock = None
        for _ in range(5):
            sock = getattr(stream, "_sock", None)
            if sock is not None:
                break
            child = getattr(stream, "raw", None)
            stream = child if child is not None else getattr(stream, "fp", None)
            if stream is None:
                break
        _require(sock is not None and callable(getattr(response, "read1", None)),
            "managed_control_deadline_transport_unavailable")
        interruption = _REQUEST_INTERRUPTION.get()
        unregister = (lambda: None) if interruption is None else interruption.register(
            lambda: sock.shutdown(socket.SHUT_RDWR))
        try:
            raw = bytearray()
            while len(raw) <= MAX_FRAME_BYTES:
                sock.settimeout(self._transport_timeout())
                chunk = response.read1(min(65536, MAX_FRAME_BYTES + 1 - len(raw)))
                self._transport_timeout()
                if not chunk:
                    return bytes(raw)
                raw.extend(chunk)
                # HTTPResponse can already close the descriptor at Content-Length.
                if getattr(response, "length", None) == 0 or response.isclosed():
                    return bytes(raw)
            return bytes(raw)
        finally:
            unregister()

    def _request(self, path: str, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        """Retry only proxy failures on exact, idempotent mailbox requests.

        The durable caller still owns the envelope and ambiguous outcome. Nothing
        here signs, refreshes TTL, advances a sequence, or replays native work.
        Exhaustion retains the HTTP error, so the pump's existing reconciliation
        terminal applies instead of requeueing indefinitely. Owner and gateway
        command deadlines and cancellation remain authoritative.
        """
        if path not in _HTTP_REPLAY_PATHS:
            return self._request_once(path, envelope)
        deadline = time.monotonic() + HTTP_RETRY_WINDOW_SECONDS
        parent = _REQUEST_DEADLINE.get()
        if parent is not None:
            deadline = min(deadline, parent)
        delay = HTTP_RETRY_INITIAL_SECONDS
        last = None
        # Freeze once: even a caller mutating its input cannot change a retry.
        envelope = _detached(envelope)
        with request_deadline(deadline, interruption=_REQUEST_INTERRUPTION.get()):
            for attempt in range(HTTP_RETRY_ATTEMPTS):
                if last is not None and time.monotonic() >= deadline:
                    raise last
                try:
                    return self._request_once(path, envelope)
                except _TransientHTTPError as error:
                    last = error
                except ManagedControlError as error:
                    if last is not None and error.code == 'managed_control_response_timeout':
                        raise last from error
                    raise
                remaining = deadline - time.monotonic()
                if attempt + 1 == HTTP_RETRY_ATTEMPTS or remaining <= 0:
                    raise last
                wait = min(delay, remaining)
                interruption = _REQUEST_INTERRUPTION.get()
                if interruption is None:
                    time.sleep(wait)
                else:
                    wake = threading.Event()
                    unregister = interruption.register(wake.set)
                    try:
                        wake.wait(wait)
                    finally:
                        unregister()
                    _require(not interruption.is_set(), 'managed_control_command_interrupted')
                delay = min(HTTP_RETRY_MAX_SECONDS, delay * 2)
        assert last is not None  # all eight attempts failed
        raise last

    def _request_once(self, path: str, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = canonical_json_bytes(dict(envelope))
        request = Request(
            self.base_url + path,
            data=raw,
            method="POST",
            headers=with_user_agent({"Content-Type": "application/json", "Accept": "application/json"}),
        )
        try:
            with self._opener.open(request, timeout=self._transport_timeout()) as response:
                response_raw = self._read_response(response)
                content_type = response.headers.get("Content-Type")
        except HTTPError as exc:
            try:
                error_raw = self._read_response(exc)
                content_type = exc.headers.get("Content-Type")
            except (OSError, URLError, ManagedControlError) as error:
                # Incomplete HTTP error bodies cannot grant retry authority; keep
                # the known status/path even if the response stalls at the deadline.
                code = ('managed_control_command_interrupted'
                        if getattr(error, 'code', None) == 'managed_control_command_interrupted'
                        else 'managed_control_remote_error')
                raise ManagedControlError(code, status=exc.code, request_path=path) from error
            finally:
                exc.close()
            code = "managed_control_remote_error"
            structured = False
            try:
                # This permissive parse can only DENY a retry, never accept a
                # receipt or an authority code. Noncanonical JSON refusals also
                # stay fatal; the canonical validator below still owns codes.
                document = json.loads(error_raw) if len(error_raw) <= MAX_FRAME_BYTES else None
                structured = isinstance(document, Mapping) and 'error' in document
            except (ValueError, RecursionError):
                pass
            try:
                error = _canonical_loads(error_raw)
                candidate = error.get("error")
                # A JSON refusal is not a proxy retry grant, even when a proxy
                # rewrites Content-Type. Preserve the existing public code rules.
                structured = 'error' in error
                if (content_type == "application/json" and isinstance(candidate, Mapping)
                        and set(candidate) == {"code"}):
                    code = str(candidate["code"])
            except ManagedControlError:
                pass
            error_type = (_TransientHTTPError if not structured and exc.code in _HTTP_RETRY_STATUSES
                          else ManagedControlError)
            raise error_type(code, status=exc.code, request_path=path) from exc
        except (OSError, URLError) as exc:
            # The route and the inner cause are both part of the bounded failure
            # identity: without them a fetch failure during a multi-request start
            # is unattributable, and a fail-closed library guard such as
            # `relay_dns_nonpublic` becomes indistinguishable from a network drop.
            raise ManagedControlError("managed_control_unreachable", request_path=path,
                cause_code=_bounded_unreachable_cause(exc)) from exc
        _require(content_type == "application/json", "managed_control_response_invalid")
        _require(len(response_raw) <= MAX_FRAME_BYTES, "managed_control_frame_too_large")
        return _canonical_loads(response_raw)

    def _verified_response(
        self,
        envelope: Mapping[str, Any],
        *,
        protocol: str,
        fields: frozenset[str],
        request_id: str,
        target: Mapping[str, Any],
        run_id: str,
        deployment_id: str,
    ) -> dict[str, Any]:
        try:
            statement = _verify_signed(
                envelope,
                expected_protocol=protocol,
                expected_key_record=self.trusted_owner_key_record,
                expected_endpoint_id=str(envelope.get("statement", {}).get("owner_endpoint_id")),
                now=self._clock(),
            )
            if protocol in {ACK_RECEIPT_PROTOCOL, RESULT_RECEIPT_PROTOCOL}:
                fields = _diagnostic_fields(envelope, fields)
                _target_diagnostic(statement)
            _require(set(statement) == fields, "managed_control_response_invalid")
            _require(
                statement["request_id"] == request_id
                and statement["swarm_id"] == self.swarm_id
                and statement["owner_node_id"] == self.owner_node_id
                and statement["owner_endpoint_id"]
                == envelope["signature"]["signer_endpoint_id"],
                "managed_control_response_invalid",
            )
            expected_binding = _binding_from_member(target, run_id, deployment_id)
            _require(
                all(statement[field] == value for field, value in expected_binding.items()),
                "managed_control_response_invalid",
            )
            return statement
        except ManagedControlError as exc:
            if exc.code in {
                "managed_control_signature_invalid",
                "managed_control_key_pin_mismatch",
            }:
                raise ManagedControlError(
                    "managed_control_response_signature_invalid"
                ) from exc
            raise
        except Exception as exc:
            raise ManagedControlError("managed_control_response_invalid") from exc

    def enqueue(
        self,
        *,
        target: Mapping[str, Any],
        native_command: Mapping[str, Any],
        sequence: int,
        nonce: str,
        request_id: str,
        ttl_seconds: float = 60.0,
    ) -> dict[str, Any]:
        statement = self.owner_command_statement(
            target=target,
            native_command=native_command,
            sequence=sequence,
            nonce=nonce,
            request_id=request_id,
            ttl_seconds=ttl_seconds,
        )
        response = self._request(
            "/managed-control/enqueue", self._signed_outbound(statement)
        )
        return self._verified_response(
            response,
            protocol=ENQUEUE_RECEIPT_PROTOCOL,
            fields=_ENQUEUE_RECEIPT_FIELDS,
            request_id=request_id,
            target=target,
            run_id=statement["run_id"],
            deployment_id=statement["deployment_id"],
        )

    def result(
        self, *, target: Mapping[str, Any], native_command: Mapping[str, Any],
        sequence: int, nonce: str, request_id: str, ttl_seconds: float = 60.0,
    ) -> dict[str, Any]:
        statement = self.owner_command_statement(target=target, native_command=native_command,
            sequence=sequence, nonce=nonce, request_id=request_id, ttl_seconds=ttl_seconds)
        statement["protocol"] = OWNER_RESULT_PROTOCOL
        response = self._request("/managed-control/result", self._signed_outbound(statement))
        result = self._verified_response(response, protocol=RESULT_RECEIPT_PROTOCOL,
            fields=_RESULT_RECEIPT_FIELDS, request_id=request_id, target=target,
            run_id=statement["run_id"], deployment_id=statement["deployment_id"])
        _require(result["command_id"] == native_command["command_id"]
            and result["command_digest"] == _document_digest(native_command),
            "managed_control_response_invalid")
        state = result["state"]
        _require(state in {"pending", "claimed", "reconciliation_required", "completed", "failed", "expired"},
            "managed_control_response_invalid")
        if state in {"completed", "failed"}:
            native = _validate_native_response(result["native_response"],
                command_id=native_command["command_id"], node_id=result["target_node_id"], disposition=state)
            _require(_document_digest(native) == result["native_response_digest"],
                "managed_control_response_invalid")
        else:
            _require(result["native_response"] is None and result["native_response_digest"] is None,
                "managed_control_response_invalid")
        return result

    def token_progress(self, **kwargs):
        from .token_receipt_mailbox import send_token_progress
        return send_token_progress(self, **kwargs)

    def poll(
        self,
        *,
        target: Mapping[str, Any],
        run_id: str,
        deployment_id: str,
        sequence: int,
        nonce: str,
        request_id: str,
        ttl_seconds: float = 60.0,
        active_claims: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        statement = self._target_statement(
            protocol=TARGET_POLL_PROTOCOL_V2 if active_claims is not None else TARGET_POLL_PROTOCOL,
            additions=None if active_claims is None else {"active_claims": dict(active_claims)},
            target=target,
            run_id=run_id,
            deployment_id=deployment_id,
            sequence=sequence,
            nonce=nonce,
            request_id=request_id,
            ttl_seconds=ttl_seconds,
        )
        response = self._request(
            "/managed-control/poll", self._signed_outbound(statement)
        )
        verified = self._verified_response(
            response,
            protocol=POLL_RECEIPT_PROTOCOL,
            fields=_POLL_RECEIPT_FIELDS,
            request_id=request_id,
            target=target,
            run_id=run_id,
            deployment_id=deployment_id,
        )
        kind = verified["delivery_kind"]
        _require(
            kind in {"empty", "command", "reconciliation_required"},
            "managed_control_response_invalid",
        )
        if kind == "command":
            _require(
                isinstance(verified["command_id"], str)
                and isinstance(verified["claim_token"], str)
                and isinstance(verified["command_envelope"], Mapping),
                "managed_control_response_invalid",
            )
            command_statement = _verify_signed(
                verified["command_envelope"],
                expected_protocol=OWNER_COMMAND_PROTOCOL,
                expected_key_record=self.trusted_owner_key_record,
                expected_endpoint_id=verified["owner_endpoint_id"],
                now=self._clock(),
            )
            _require(
                command_statement["native_command"]["command_id"]
                == verified["command_id"]
                and all(
                    command_statement[field] == verified[field]
                    for field in _BINDING_FIELDS
                ),
                "managed_control_response_invalid",
            )
            validate_native_command(command_statement["native_command"])
        elif kind == "empty":
            _require(
                verified["command_id"] is None
                and verified["claim_token"] is None
                and verified["command_envelope"] is None,
                "managed_control_response_invalid",
            )
        else:
            _require(
                isinstance(verified["command_id"], str)
                and isinstance(verified["claim_token"], str)
                and verified["command_envelope"] is None,
                "managed_control_response_invalid",
            )
        return verified

    def ack(
        self,
        *,
        target: Mapping[str, Any],
        run_id: str,
        deployment_id: str,
        command_id: str,
        claim_token: str,
        disposition: str,
        native_response: Mapping[str, Any] | None,
        sequence: int,
        nonce: str,
        request_id: str,
        ttl_seconds: float = 60.0,
        target_error_code: str | None = None,
    ) -> dict[str, Any]:
        statement = self._target_statement(
            protocol=TARGET_ACK_PROTOCOL,
            target=target,
            run_id=run_id,
            deployment_id=deployment_id,
            sequence=sequence,
            nonce=nonce,
            request_id=request_id,
            ttl_seconds=ttl_seconds,
            additions={
                "command_id": command_id,
                "claim_token": claim_token,
                "disposition": disposition,
                "native_response": native_response,
                **({} if target_error_code is None else {'target_error_code': target_error_code}),
            },
        )
        response = self._request(
            "/managed-control/ack", self._signed_outbound(statement)
        )
        return self._verified_response(
            response,
            protocol=ACK_RECEIPT_PROTOCOL,
            fields=_ACK_RECEIPT_FIELDS,
            request_id=request_id,
            target=target,
            run_id=run_id,
            deployment_id=deployment_id,
        )


class _ManagedControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], mailbox: ManagedControlMailbox) -> None:
        self.mailbox = mailbox
        super().__init__(address, _ManagedControlRequestHandler)


class _ManagedControlRequestHandler(BaseHTTPRequestHandler):
    server: _ManagedControlHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        try:
            _require(
                self.headers.get("Content-Type") == "application/json",
                "managed_control_content_type_invalid",
            )
            length_text = self.headers.get("Content-Length")
            _require(
                isinstance(length_text, str) and length_text.isdigit(),
                "managed_control_frame_invalid",
            )
            length = int(length_text)
            _require(0 < length <= MAX_FRAME_BYTES, "managed_control_frame_too_large")
            body = _canonical_loads(self.rfile.read(length))
            status, response = self.server.mailbox.handle_http("POST", self.path, body)
            self._send(status, response)
        except ManagedControlError as exc:
            conflict = {
                "managed_control_conflicting_retry",
                "managed_control_sequence_replayed",
                "managed_control_nonce_replayed",
                "managed_control_member_stale",
            }
            authentication = {
                "managed_control_signature_invalid",
                "managed_control_key_pin_mismatch",
                "managed_control_message_expired",
                "managed_control_message_from_future",
            }
            status = (
                HTTPStatus.CONFLICT
                if exc.code in conflict
                else HTTPStatus.UNAUTHORIZED
                if exc.code in authentication
                else HTTPStatus.BAD_REQUEST
            )
            self._send(status, {"error": {"code": exc.code}})
        except BaseException:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"code": "managed_control_internal_error"}},
            )

    def _send(self, status: int, document: Mapping[str, Any]) -> None:
        raw = canonical_json_bytes(dict(document))
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()
        self.close_connection = True


class LocalTLSManagedControlServer:
    """Explicit loopback-only local TLS test seam; never a public listener."""

    def __init__(
        self,
        mailbox: ManagedControlMailbox,
        *,
        certificate: Path,
        private_key: Path,
    ) -> None:
        server = _ManagedControlHTTPServer(("127.0.0.1", 0), mailbox)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(str(certificate), str(private_key))
            server.socket = context.wrap_socket(server.socket, server_side=True)
        except BaseException:
            server.server_close()
            raise
        self._server = server
        self._thread: threading.Thread | None = None
        self.base_url = f"https://127.0.0.1:{server.server_address[1]}"

    def start(self) -> "LocalTLSManagedControlServer":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="mycelium-local-managed-control-tls",
                daemon=True,
            )
            self._thread.start()
        return self

    def replace_mailbox_for_test(self, mailbox: ManagedControlMailbox) -> None:
        """Test-only crash/restart seam; no request may be active during replacement."""

        self._server.mailbox = mailbox

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        thread.join(timeout=5.0)
        if thread.is_alive():
            raise RuntimeError("managed control TLS server cleanup failed")
        self._thread = None

    def __enter__(self) -> "LocalTLSManagedControlServer":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


__all__ = [
    "ACK_RECEIPT_PROTOCOL",
    "CONTROL_COMMAND_PROTOCOL",
    "ENQUEUE_RECEIPT_PROTOCOL",
    "LocalTLSManagedControlServer",
    "ManagedControlClient",
    "ManagedControlError",
    "ManagedControlMailbox",
    "OWNER_COMMAND_PROTOCOL",
    "POLL_RECEIPT_PROTOCOL",
    "SIGNED_ENVELOPE_PROTOCOL",
    "TARGET_ACK_PROTOCOL",
    "TARGET_POLL_PROTOCOL",
    "sign_managed_statement",
    "validate_native_command",
]
