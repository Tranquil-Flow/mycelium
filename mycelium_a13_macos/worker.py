# SPDX-License-Identifier: AGPL-3.0-only
"""App-owned native node process with bounded, fail-closed supervision.

This is a local execution boundary, not membership authentication or a load
permit producer.  The caller supplies current-membership and exact command
load-grant checks.  The native process boundary owns correlated concurrent
waiters and priority cleanup writes; this adapter never serializes a complete
request/response exchange or manufactures a native response for a local
admission refusal.

Source tests exercise only real native hello/stop and pre-load semantic errors;
model execution and physical qualification remain outside this module's tests.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping

import mycelium_host_platform

from .child_environment import source_mode_environment
from mycelium_qualification.evidence import canonical_json_bytes

from .resources import observe_policy, policy_violation

_CLEANUP_RESERVE_SECONDS = 10
_STARTUP_TIMEOUT_SECONDS = 30.0
"""Bounded allowance for ONE spawn+hello handshake.

The handshake is startup, not model execution, and it must not be charged
against the model's wall budget.  It used to be bounded by the managed
session's response timeout, which ``_native_budgets`` derives from that budget:
at ``max_wall_seconds=12`` the 10 s cleanup reserve leaves a 2.0 s bound, while a
cold native spawn plus its hello needs longer than that on a loaded host.  The
L2 runs ``pytest -n8``, so the suite generates that load itself (the batch-2 L2
sat at load1 ~57) and no admission rule can prevent it: measured there, the
hello exhausted its 2.0 s bound and ``start()`` failed closed with
``node_response_timeout`` before the worker ever executed anything.

So startup gets its own bounded allowance -- far above the handshake's real
cost (quiet: ~0.4 s) and far below the managed session cap
(``MAX_SESSION_TIMEOUT_SECONDS * _NATIVE_RESPONSE_TRANSIT_MARGIN``), so a wedged
child still fails closed instead of hanging ``start()``.
"""
_NATIVE_PROTOCOL = "mycelium.physical_node_control.v1"

# Closed private-diagnostic vocabulary; unknown/free-form errors remain unrecorded.
_PRIVATE_CAUSE_CODES = frozenset({
    'model_execution_not_authorized',
    'model_load_not_admitted',
    'resource_network_policy_violation',
    'resource_observation_invalid',
    'resource_policy_invalid',
    'resource_power_policy_violation',
    'resource_thermal_policy_violation',
    'worker_artifact_root_invalid',
    'worker_capacity_root_invalid',
    'worker_command_invalid',
    'worker_command_limit_exceeded',
    'worker_command_not_allowed',
    'worker_command_reconciliation_required',
    'worker_configuration_invalid',
    'worker_disk_limit_exceeded',
    'worker_guard_failed',
    'worker_identity_invalid',
    'worker_limits_invalid',
    'worker_membership_not_current',
    'worker_memory_limit_exceeded',
    'worker_memory_observation_failed',
    'worker_native_exited',
    'worker_native_response_invalid',
    'worker_native_start_failed',
    'worker_not_running',
    'worker_owner_stopped',
    'worker_replacement_state_unavailable',
    'worker_resource_observation_failed',
    'worker_restart_requires_new_instance',
    'worker_storage_observation_failed',
    'worker_wall_limit_exceeded',
})


class WorkerError(RuntimeError):
    """Stable local worker admission/supervision error."""

    def __init__(self, code: str) -> None:
        self.code = code
        # Numbers behind a limit refusal, when the guard has them: the code alone
        # cannot say whether the bound or the accounting is wrong.
        self.limit_detail: dict[str, int] | None = None
        # Why a memory observation refused, when the guard knows: a bare code
        # made the batch-2 L2 triage (2026-09-21) depend on re-running the host.
        self.observation_detail: dict[str, Any] | None = None
        super().__init__(code)


@dataclass(frozen=True)
class WorkerLimits:
    max_rss_bytes: int
    max_disk_bytes: int
    max_wall_seconds: int
    max_commands: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise WorkerError("worker_limits_invalid")
        if (
            not _CLEANUP_RESERVE_SECONDS < self.max_wall_seconds <= 3600
            or self.max_commands > 10000
        ):
            raise WorkerError("worker_limits_invalid")


_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_OPERATION = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")

_NATIVE_DIAGNOSTIC_FRAME = re.compile(
    r'File "([^"]+)", line ([0-9]{1,8}), in ([A-Za-z_][A-Za-z_0-9]*|<module>)'
)
_NATIVE_DIAGNOSTIC_EXCEPTIONS = frozenset(
    {
        "IrohTransportError",
        "NodeCommandError",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "TimeoutError",
        "OSError",
        "KeyError",
        "AssertionError",
    }
)

# Bounded, allowlisted prefill-hop diagnostic.  The native prefill wait only
# observes ``transport.fatal_error``, so a hop that fails inside the deferred
# forward path is recorded there as a scoped (non-fatal) event and the operator
# sees only the generic ``prefill_completion_timeout``.  The native node prints
# one fixed-shape line that this parser reduces to an allowed key set; anything
# else in the tail (addresses, endpoint ids, keys, prompts, messages) is never
# retained.
_NATIVE_TRANSPORT_LINE = re.compile(r"^MYCELIUM-TRANSPORT-DIAG ([!-~ ]{1,400})$", re.M)
_NATIVE_TRANSPORT_COUNTER_KEYS = (
    "remote_frames_sent",
    "remote_frames_received",
    "router_frames_dispatched",
    "duplicate_frames",
)
_NATIVE_TRANSPORT_KEYS = frozenset(
    {"scoped_failure_code", *_NATIVE_TRANSPORT_COUNTER_KEYS}
)
_NATIVE_TRANSPORT_STATUS = re.compile(r"[a-z][a-z0-9_]{0,60}\Z")


def _bounded_transport_diagnostic(text: str) -> dict[str, Any] | None:
    """Reduce one ``MYCELIUM-TRANSPORT-DIAG`` line to an allowlisted verdict.

    The generic ``prefill_completion_timeout`` is a symptom: the native wait
    only polls ``transport.fatal_error``, while a hop that fails in the deferred
    forward path is recorded as a scoped, per-path, non-fatal event.  This
    recovers the transport's own verdict for that hop.

    Fails closed: a line is accepted only when it carries *exactly* the allowed
    key set with individually validated values.  Any malformed, oversized or
    unexpected input yields ``None`` -- never a partial record, never a raise.
    Only an allowlisted status token and bounded decimal counters can survive, so
    addresses, endpoint ids, keys, prompts and raw messages cannot reach here.
    """
    for match in _NATIVE_TRANSPORT_LINE.finditer(text):
        fields: dict[str, str] = {}
        for token in match.group(1).split():
            key, separator, value = token.partition("=")
            if not separator or key in fields:
                return None
            fields[key] = value
        if set(fields) != _NATIVE_TRANSPORT_KEYS:
            return None
        status = fields["scoped_failure_code"]
        if status != "none" and _NATIVE_TRANSPORT_STATUS.fullmatch(status) is None:
            return None
        counters: dict[str, int] = {}
        for name in _NATIVE_TRANSPORT_COUNTER_KEYS:
            raw_value = fields[name]
            if not raw_value.isdigit() or len(raw_value) > 12:
                return None
            counters[name] = int(raw_value)
        return {
            "protocol": "mycelium.a13.native_transport_diagnostic.v1",
            "scoped_failure_code": status,
            **counters,
        }
    return None


# Bounded, allowlisted load-proof diagnostic: on placement_load_proof_mismatch
# the native node prints the component digests of the proof it observed so the
# operator can diff them against the compiled node-N-load-proof.json.  Only
# ``sha256:`` references (or ``none``) under an exact key set survive.
_NATIVE_LOAD_PROOF_LINE = re.compile(r"^MYCELIUM-LOAD-PROOF-DIAG ([!-~ ]{1,800})$", re.M)
_NATIVE_LOAD_PROOF_KEYS = (
    "expected_load_proof_digest",
    "observed_load_proof_digest",
    "loaded_tensor_digest",
    "probe_digest",
    "runtime_identity_digest",
    "stage_pack_digest",
)
_NATIVE_LOAD_PROOF_VALUE = re.compile(r"(?:sha256:[0-9a-f]{64}|none)\Z")


def _bounded_load_proof_diagnostic(text: str) -> dict[str, Any] | None:
    """Reduce one ``MYCELIUM-LOAD-PROOF-DIAG`` line to allowlisted digests.

    Fails closed exactly like the transport line: the exact key set, each value
    a ``sha256:`` reference or ``none``; anything else yields ``None``.
    """
    for match in _NATIVE_LOAD_PROOF_LINE.finditer(text):
        fields: dict[str, str] = {}
        for token in match.group(1).split():
            key, separator, value = token.partition("=")
            if not separator or key in fields:
                return None
            fields[key] = value
        if set(fields) != set(_NATIVE_LOAD_PROOF_KEYS) or not all(
            _NATIVE_LOAD_PROOF_VALUE.fullmatch(value) for value in fields.values()
        ):
            return None
        return {
            "protocol": "mycelium.a13.native_load_proof_diagnostic.v1",
            **{key: fields[key] for key in _NATIVE_LOAD_PROOF_KEYS},
        }
    return None


def _bounded_native_diagnostics(raw: Any) -> dict[str, Any]:
    """Derive allowlisted frames and exception classes from a stderr tail.

    Never retains raw stderr, source lines, exception messages, prompts or
    credentials.  Shared by the command-failure and process-exit recorders so
    both boundaries report the same bounded shape.
    """
    text = raw[-10000:].decode("utf-8", "replace") if isinstance(raw, bytes) else ""
    frames = []
    for file, line, function in _NATIVE_DIAGNOSTIC_FRAME.findall(text):
        name = Path(file).name
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.]{0,80}[.]py", name):
            frames.append({"file": name, "line": int(line), "function": function[:80]})
    types = [
        name
        for name in re.findall(r"^([A-Za-z_][A-Za-z_0-9]*):", text, re.M)
        if name in _NATIVE_DIAGNOSTIC_EXCEPTIONS
    ]
    result: dict[str, Any] = {
        "frames": frames[-24:],
        "exception_types": types[-8:],
    }
    transport = _bounded_transport_diagnostic(text)
    if transport is not None:
        result["transport"] = transport
    load_proof = _bounded_load_proof_diagnostic(text)
    if load_proof is not None:
        result["load_proof"] = load_proof
    return result


def _native_error_code(response: Any) -> str | None:
    """The native's own bounded name for the cause, or ``None``.

    ``_validate_response`` admits an error response only when its error mapping is exactly
    ``{'code': <token matching the closed operation grammar>}``, so this field cannot carry
    free text -- and it is re-validated here so a record written by any other caller stays
    bounded too.  Dropping it is what made p74r1's ``infer_start`` failure say only that a
    command failed on a node, never why (the third occurrence of that gap).
    """
    if not isinstance(response, Mapping):
        return None
    error = response.get("error")
    if not isinstance(error, Mapping) or set(error) != {"code"}:
        return None
    code = error.get("code")
    if not isinstance(code, str) or _OPERATION.fullmatch(code) is None:
        return None
    return code


def _unavailable_code(worker: Any) -> str:
    """Precise bounded cause for a worker that cannot accept a command.

    A worker that was started and then lost its native process must not be
    reported as merely absent.  ``worker_native_exited`` is the actionable
    cause; the generic code hides why the worker is gone, which is what let an
    opaque fail-closed route stand in for a diagnosable defect.
    """
    recorded = getattr(worker, "_error", None)
    if isinstance(recorded, str) and _CODE.fullmatch(recorded):
        return recorded
    return "worker_not_running"


_NATIVE_RESPONSE_TRANSIT_MARGIN = 0.7
"""Fraction of the owner's managed-control session cap one native command may use.

The owner abandons a managed-control exchange at ``MAX_SESSION_TIMEOUT_SECONDS``
(240 s), and the native node's own prefill wait is ``command_timeout * 0.8``.  If
the native budget is pinned to the *same* 240 s constant, the native only answers
at 192 s and just 48 s is left for that answer to travel back -- target poll,
signing, envelope, owner processing.  round34 squeaked through at 193.1 s;
round35 exceeded the window, the owner recorded ``managed_control_response_timeout``
with ``resp_code: None``, and the native's real cause was discarded entirely.

Deriving the native budget as a strict fraction of the session cap guarantees the
native always answers *inside* the owner's window, so the real native code (a
transport verdict, or ``prefill_completion_timeout``) is always observable instead
of being masked by a control-plane timeout.  This is a proven budget invariant, not
a tuning guess: it comes from the documented cap plus the observed race.
"""


def _native_budgets(limits: Any) -> tuple[float, float]:
    """Native command and response budget derived from the approved wall limit.

    The canonical route budgets ``NODE_COMMAND_TIMEOUT_SECONDS`` (900) and
    ``NODE_SESSION_TIMEOUT_SECONDS`` (930), and the managed transport caps one
    session at ``MAX_SESSION_TIMEOUT_SECONDS`` (240).  A hardcoded 5 s command
    budget cannot cover the startup challenge's first *cold* distributed
    prefill, which is why ``infer_start`` raised ``TimeoutError`` and exited the
    native worker.  Derive the budget instead of inventing a number, never exceed
    the managed session cap, and stay inside it by ``_NATIVE_RESPONSE_TRANSIT_MARGIN``
    so the native's answer always reaches the owner before the owner gives up.
    """
    from .control_session import MAX_SESSION_TIMEOUT_SECONDS

    budget = float(limits.max_wall_seconds) - _CLEANUP_RESERVE_SECONDS
    capped = MAX_SESSION_TIMEOUT_SECONDS * _NATIVE_RESPONSE_TRANSIT_MARGIN
    bounded = min(budget, capped)
    if not 0 < bounded <= capped:
        raise WorkerError("worker_limits_invalid")
    return (bounded, bounded)


_CANCEL_CONTROL_FIELDS = frozenset(
    {
        "request_id",
        "deployment_id",
        "deployment_epoch",
        "qualification_digest",
        "command_id",
        "publisher_generation",
        "absolute_deadline_ms",
        "request_attempt",
        "path_id",
        "path_attempt",
        "path_digest",
        "topology_generation",
        "cancellation_generation",
        "deadline_budget_ms",
    }
)
_OPERATION_PAYLOAD_FIELDS: dict[str, tuple[frozenset[str], ...]] = {
    "hello": (frozenset(),),
    "configure": (
        frozenset(
            {
                "assignment_file",
                "artifact_report_file",
                "graph",
                "device_states",
                "load_generation",
            }
        ),
        frozenset(
            {
                "assignment_file",
                "manifest_file",
                "stage_pack_file",
                "graph",
                "device_states",
                "load_generation",
            }
        ),
        frozenset(
            {
                "assignment_file",
                "pixel_stage_pack_file",
                "graph",
                "device_states",
                "load_generation",
            }
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
    "bind_request_control": (frozenset({"request_id", "control"}),
        frozenset({"request_id", "control", "token_path"})),
    "update_request_control": (frozenset({"request_id", "control"}),),
    "infer_start": (
        frozenset({"request"}),
        frozenset({"request", "excluded_placement_ids"}),
        frozenset({"request", "control", "path_manifest"}),
        frozenset({"request", "control", "path_manifest", "token_grant"}),
        frozenset(
            {"request", "control", "path_manifest", "excluded_placement_ids"}
        ),
    ),
    "infer_decode": (
        frozenset({"request_id", "count"}),
        frozenset({"request_id", "count", "control"}),
    ),
    "infer_cancel": (
        frozenset({"request_id"}),
        _CANCEL_CONTROL_FIELDS,
    ),
    "infer_cancel_wait": (_CANCEL_CONTROL_FIELDS,),
    "rotate": (frozenset({"peer"}),),
    "stop": (frozenset(),),
    "ack_token_progress": (frozenset({"ack"}),),
}
_ALLOWED = frozenset(_OPERATION_PAYLOAD_FIELDS)
_EXACT_GRANT_COMMANDS = frozenset(
    {"configure", "start", "infer_start", "infer_decode", "rotate"}
)
_REQUIRES_ADMITTED_LOAD = frozenset(
    {
        "start",
        "install_token_path",
        "bind_request_control",
        "update_request_control",
        "infer_start",
        "infer_decode",
        "infer_cancel",
        "infer_cancel_wait",
        "rotate",
    }
)
_FORBIDDEN_EXECUTION_KEYS = frozenset(
    {
        "argv",
        "cwd",
        "exec",
        "executable",
        "executable_path",
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


def _reject_execution_inputs(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkerError("worker_command_invalid")
            if key.lower().replace("-", "_") in _FORBIDDEN_EXECUTION_KEYS:
                raise WorkerError("worker_command_invalid")
            _reject_execution_inputs(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_execution_inputs(item)


def _detached_json_object(
    value: Mapping[str, Any], *, code: str = "worker_command_invalid"
) -> dict[str, Any]:
    try:
        encoded = canonical_json_bytes(dict(value))
        detached = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WorkerError(code) from exc
    if not isinstance(detached, dict):
        raise WorkerError(code)
    return detached


def _native_response(
    *, node_id: str, command_id: str, ok: bool, value: Any
) -> dict[str, Any]:
    response = {
        "protocol": _NATIVE_PROTOCOL,
        "command_id": command_id,
        "node_id": node_id,
        "ok": ok,
        "route_ready": False,
    }
    if ok:
        response["result"] = value
    else:
        response["error"] = {"code": value}
    return response


class _ConcurrentNativeSession:
    """Expose validated native envelopes over PhysicalNodeProcess demultiplexing."""

    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        node_id: str,
        run_id: str,
        deployment_id: str,
        timeout_seconds: float,
    ) -> None:
        # Import the native owner lazily so importing the packaged launcher has
        # no process side effect.  PhysicalNodeProcess is the existing hardened
        # writer/reader boundary; do not add a second pipe reader here.
        import mycelium_node.process as process_module

        self._process_module = process_module
        self._process = process_module.PhysicalNodeProcess(
            command=argv,
            node_id=node_id,
            run_id=run_id,
            deployment_id=deployment_id,
            response_timeout_seconds=timeout_seconds,
            environment=source_mode_environment(),
        )
        self.node_id = node_id

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def birth_token(self) -> str | None:
        # PhysicalNodeProcess already captures PID birth for ownership checks;
        # reuse that captured token, never inventory an arbitrary PID here.
        owner = getattr(self._process, "_pre_exec_ownership", None)
        value = getattr(owner, "start_token", None)
        return value if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}(?::[0-9]{1,20})?", value) else None

    @property
    def launch_timing(self) -> dict[str, Any] | None:
        return getattr(self._process, "launch_timing", None)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def win32_group_resident_bytes(self) -> int:
        return int(self._process.win32_job_resident_bytes())

    def win32_group_owned(self) -> bool:
        return bool(self._process.win32_job_owned())

    @property
    def stderr(self) -> bytes:
        return self._process.stderr_tail.encode("utf-8", errors="replace")

    def _send(
        self,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
        deadline_monotonic_s: float | None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "terminate_on_timeout": False,
            "command_id": command_id,
        }
        if deadline_monotonic_s is not None:
            remaining = deadline_monotonic_s - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("command_deadline_exceeded")
            options["timeout_seconds"] = remaining
        elif timeout_seconds is not None:
            # A phase-specific bound -- the startup handshake -- or the session
            # default, which is derived from the model's wall budget and must
            # not be the only bound a spawn+hello has to fit inside.
            options["timeout_seconds"] = timeout_seconds
        try:
            result = self._process.command(command, payload, **options)
        except self._process_module._RemoteNodeError as exc:
            # This private subclass is the native boundary's only distinction
            # between an authenticated node rejection and local pipe/process
            # failure.  Rebuild only the fixed envelope fields already verified
            # by PhysicalNodeProcess; the error code itself is native-authored.
            return _native_response(
                node_id=self.node_id,
                command_id=command_id,
                ok=False,
                value=exc.code,
            )
        return _native_response(
            node_id=self.node_id,
            command_id=command_id,
            ok=True,
            value=result,
        )

    def send(
        self, *, command_id: str, command: str, payload: Mapping[str, Any],
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self._send(
            command_id=command_id,
            command=command,
            payload=payload,
            deadline_monotonic_s=None,
            timeout_seconds=timeout_seconds,
        )

    def send_before(
        self,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
        deadline_monotonic_s: float,
    ) -> dict[str, Any]:
        return self._send(
            command_id=command_id,
            command=command,
            payload=payload,
            deadline_monotonic_s=deadline_monotonic_s,
            timeout_seconds=None,
        )

    def open_progress(self, *, command_id: str, cursor: Any,
                      deadline_monotonic_s: float, capacity: int = 64) -> Any:
        """Native receipt seam only; no mailbox forwarding or feature negotiation."""
        return self._process.open_progress(
            command_id=command_id, cursor=cursor,
            deadline_monotonic_s=deadline_monotonic_s, capacity=capacity,
        )

    def close_progress(self, command_id: str, *, code: str = "token_progress_closed") -> None:
        self._process.close_progress(command_id, code=code)

    def interrupt_command(self, command_id: str, *, code: str) -> bool:
        return self._process.interrupt_command(command_id, code=code)

    def close(self) -> None:
        self._process.close()


class ManagedWorker:
    # Memory observation (`_group_rss`): the per-attempt bound, plus the bounded
    # tolerance for a host-loaded stall that the batch-2 L2 exposed.
    _OBSERVATION_TIMEOUT_S = 2.0
    _OBSERVATION_ATTEMPTS = 3
    _OBSERVATION_BACKOFF_S = 0.05

    def __init__(
        self,
        *,
        node_id: str,
        run_id: str,
        deployment_id: str,
        artifact_root: Path,
        socket_root: Path,
        sidecar_binary: Path,
        resource_policy: Mapping[str, Any],
        limits: WorkerLimits,
        membership_current: Callable[[], bool],
        authorize_load: Callable[[Mapping[str, Any]], bool] | None,
        endpoint_secret_file: Path | None = None,
        token_policy_trust_file: Path | None = None,
        token_policy_member: Callable[[], Mapping[str, Any]] | None = None,
        token_policy_refresh: Callable[[], Any] | None = None,
        capacity_root: Path | None = None,
        _test_observer: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        if any(
            not isinstance(value, str) or not _SEGMENT.fullmatch(value)
            for value in (node_id, run_id, deployment_id)
        ):
            raise WorkerError("worker_identity_invalid")
        if not isinstance(limits, WorkerLimits) or not callable(membership_current):
            raise WorkerError("worker_configuration_invalid")
        if authorize_load is not None and not callable(authorize_load):
            raise WorkerError("worker_configuration_invalid")
        self.node_id, self.run_id, self.deployment_id = (
            node_id,
            run_id,
            deployment_id,
        )
        self.artifact_root = Path(artifact_root).absolute()
        if self.artifact_root.is_symlink() or not self.artifact_root.is_dir():
            raise WorkerError("worker_artifact_root_invalid")
        root_info = self.artifact_root.lstat()
        self._root_identity = (root_info.st_dev, root_info.st_ino)
        self.socket_root = Path(socket_root).absolute()
        self.sidecar_binary = Path(sidecar_binary).absolute()
        self.capacity_root = None if capacity_root is None else Path(capacity_root)
        if self.capacity_root is not None and mycelium_host_platform.is_win32():
            try:
                if (not self.capacity_root.is_absolute()
                    or self.capacity_root.resolve(strict=True)!=self.capacity_root):
                    raise WorkerError("worker_capacity_root_invalid")
                with mycelium_host_platform.private_directory(self.capacity_root, create=False) as lease:
                    if not lease.exists:
                        raise WorkerError("worker_capacity_root_invalid")
            except (OSError, mycelium_host_platform.HostPlatformError):
                raise WorkerError("worker_capacity_root_invalid") from None
        elif self.capacity_root is not None:
            try:
                info=self.capacity_root.stat()
                if (not self.capacity_root.is_absolute()
                    or self.capacity_root.resolve(strict=True)!=self.capacity_root
                    or not self.capacity_root.is_dir() or info.st_uid!=os.getuid()
                    or info.st_mode & 0o777 != 0o700):
                    raise WorkerError("worker_capacity_root_invalid")
            except OSError:
                raise WorkerError("worker_capacity_root_invalid") from None
        self.endpoint_secret_file = endpoint_secret_file
        self.token_policy_trust_file = token_policy_trust_file
        self.resource_policy = dict(resource_policy)
        self.limits = limits
        self._membership_current = membership_current
        self._token_policy = None
        if token_policy_trust_file is not None:
            from .token_policy import TargetTokenPolicy
            self._token_policy = TargetTokenPolicy(path=token_policy_trust_file,
                run_id=run_id, deployment_id=deployment_id, node_id=node_id,
                current=membership_current, member=token_policy_member, refresh=token_policy_refresh)
        self._authorize_load = authorize_load
        self._observe = _test_observer or observe_policy
        self._session: Any = None
        self.pid: int | None = None
        self._group_owned = False
        self._closed = threading.Event()
        self._monitor: threading.Thread | None = None
        self._deadline_timer: threading.Timer | None = None
        self._started_at: float | None = None
        self._error: str | None = None
        self._error_detail: dict[str, Any] | None = None
        # Private provenance only. The first terminal observer captures the
        # pre-cleanup return code; close() may subsequently reap the process.
        self._started_at_utc_ms: int | None = None
        self._birth_token: str | None = None
        self._shutdown_actor: str | None = None
        self._shutdown_error_code: str | None = None
        self._observed_returncode: int | None = None
        self._command_count = 0
        self._commands: set[str] = set()
        self._pending_commands: set[str] = set()
        # One admitted execution owns every replacement's wall/command budget.
        self._budget_owner = self
        self._owner_stop_requested = False
        self._load_admitted = False
        # Even a lost configure acknowledgement may have loaded native state.
        # Such a runtime needs a fresh run, never same-run replay or continuity.
        self._configure_dispatched = False
        self._account_lock = threading.RLock()
        self._guard_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._cleanup_complete = False
        self._starting = False
        self._startup_cleanup_unproven = False
        self._startup_cleanup_owner: Any | None = None
        self._last_observation: dict[str, str] | None = None

    @staticmethod
    def _deadline_check(deadline_monotonic_s: float | None) -> None:
        if deadline_monotonic_s is not None and time.monotonic() >= deadline_monotonic_s:
            raise TimeoutError("command_deadline_exceeded")

    def _check(self, deadline_monotonic_s: float | None = None) -> None:
        # Guard observations may be relatively expensive, but they serialize
        # only with other guard observations—not with native response waits.
        if deadline_monotonic_s is None:
            self._guard_lock.acquire()
        else:
            remaining = deadline_monotonic_s - time.monotonic()
            if remaining <= 0 or not self._guard_lock.acquire(timeout=remaining):
                raise TimeoutError("command_deadline_exceeded")
        try:
            try:
                current = self._membership_current()
                if current is True and self._token_policy is not None:
                    current = self._token_policy.current()
            except Exception:
                current = False
            self._deadline_check(deadline_monotonic_s)
            if current is not True:
                raise WorkerError("worker_membership_not_current")
            try:
                observation = dict(self._observe())
            except Exception:
                raise WorkerError("worker_resource_observation_failed") from None
            self._deadline_check(deadline_monotonic_s)
            self._last_observation = observation
            violation = policy_violation(self.resource_policy, observation)
            if violation:
                raise WorkerError(violation)
            if (
                self._budget_owner._started_at is not None
                and time.monotonic() - self._budget_owner._started_at
                >= self.limits.max_wall_seconds - _CLEANUP_RESERVE_SECONDS
            ):
                raise WorkerError("worker_wall_limit_exceeded")
            total = 0
            count = 0
            try:
                info = self.artifact_root.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or (info.st_dev, info.st_ino) != self._root_identity
                ):
                    raise WorkerError("worker_storage_observation_failed")
                for root, dirs, files in os.walk(self.artifact_root, followlinks=False):
                    for name in dirs + files:
                        self._deadline_check(deadline_monotonic_s)
                        entry = Path(root) / name
                        info = entry.lstat()
                        count += 1
                        if count > 10000 or stat.S_ISLNK(info.st_mode):
                            raise WorkerError("worker_storage_observation_failed")
                        if stat.S_ISREG(info.st_mode):
                            total += info.st_size
                        elif not stat.S_ISDIR(info.st_mode):
                            raise WorkerError("worker_storage_observation_failed")
                        if total > self.limits.max_disk_bytes:
                            raise WorkerError("worker_disk_limit_exceeded")
            except OSError:
                raise WorkerError("worker_storage_observation_failed") from None
            if self._group_owned:
                rss = self._group_rss(deadline_monotonic_s)
                if rss > self.limits.max_rss_bytes:
                    error = WorkerError("worker_memory_limit_exceeded")
                    # The guard measures the process GROUP, but the resource
                    # proposal was derived from the heaviest worker stage peak
                    # alone -- so the overshoot is the number that decides
                    # whether the policy or the accounting is wrong.  Record it:
                    # 66r1 (2026-09-16) tripped this guard with no RSS anywhere,
                    # leaving the size of the gap unknowable after the fact.
                    error.limit_detail = {
                        "observed_group_rss_bytes": int(rss),
                        "max_rss_bytes": int(self.limits.max_rss_bytes),
                        "overshoot_bytes": int(rss - self.limits.max_rss_bytes),
                    }
                    raise error
        finally:
            self._guard_lock.release()

    def _group_rss(self, deadline_monotonic_s: float | None = None) -> int:
        # Numbers only: never collect other processes' argv or environment.
        #
        # A host under load can stall a cheap `/bin/ps` past the per-attempt
        # bound (or delay this thread past it) without saying anything about the
        # worker: the batch-2 L2 on 2026-09-21 at load1 ~57 stalled twice out of
        # thousands of observations that normally return in ~50 ms, and each
        # stall killed a healthy worker with worker_memory_observation_failed.
        # So the per-attempt bound stays, but a stalled attempt is retried a
        # bounded number of times -- a fresh call is a new observation, not a
        # repeat of a failing one.  The caller's deadline still wins, exhausting
        # the attempts still refuses (an unobservable host must never look like
        # a worker at zero RSS), and a report we cannot parse is our own
        # assumption breaking, so it refuses instead of under-counting.
        if mycelium_host_platform.is_win32():
            return self._win32_group_rss(deadline_monotonic_s)
        attempts = 0
        while attempts < self._OBSERVATION_ATTEMPTS:
            attempts += 1
            timeout = self._OBSERVATION_TIMEOUT_S
            if deadline_monotonic_s is not None:
                remaining = deadline_monotonic_s - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("command_deadline_exceeded")
                timeout = min(timeout, remaining)
            try:
                report = subprocess.run(
                    ["/bin/ps", "-axo", "pid=,pgid=,rss="],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=True,
                )
            except subprocess.TimeoutExpired:
                if (
                    deadline_monotonic_s is not None
                    and time.monotonic() >= deadline_monotonic_s
                ):
                    raise TimeoutError("command_deadline_exceeded") from None
                time.sleep(self._OBSERVATION_BACKOFF_S)
                continue
            except Exception:
                # `/bin/ps` could not be run at all: not a scheduling hiccup.
                raise WorkerError("worker_memory_observation_failed") from None
            total = 0
            try:
                for line in report.stdout.splitlines():
                    self._deadline_check(deadline_monotonic_s)
                    fields = line.split()
                    if len(fields) != 3:
                        raise ValueError()
                    _pid, pgid, kib = map(int, fields)
                    if pgid == self.pid:
                        total += kib * 1024
            except TimeoutError:
                raise
            except ValueError:
                raise WorkerError("worker_memory_observation_failed") from None
            return total
        error = WorkerError("worker_memory_observation_failed")
        error.observation_detail = {
            "attempts": attempts,
            "per_attempt_timeout_seconds": self._OBSERVATION_TIMEOUT_S,
            "deadline_bound": deadline_monotonic_s is not None,
        }
        raise error

    def _win32_group_rss(self, deadline_monotonic_s: float | None = None) -> int:
        """Windows: the node's process group is its kill-on-close job object.

        Every process in the job is summed by working set; an unobservable job
        refuses rather than reading as zero.
        """

        self._deadline_check(deadline_monotonic_s)
        session = self._session
        if session is None:
            raise WorkerError("worker_memory_observation_failed")
        try:
            return int(session.win32_group_resident_bytes())
        except Exception:
            raise WorkerError("worker_memory_observation_failed") from None

    def _native_argv(self) -> tuple[str, ...]:
        """Exact native-node argv, with the derived command budget.

        Extracted so the launched budget is inspectable without spawning a
        process; the previous inline construction hardcoded 5 s.
        """
        command_timeout, _ = _native_budgets(self.limits)
        args = (
            "--node-id",
            self.node_id,
            "--run-id",
            self.run_id,
            "--deployment-id",
            self.deployment_id,
            "--artifact-root",
            str(self.artifact_root),
            "--socket-root",
            str(self.socket_root),
            "--sidecar-binary",
            str(self.sidecar_binary),
            "--command-timeout",
            str(int(command_timeout)),
        )
        if self.capacity_root is not None:
            args += ("--capacity-root", str(self.capacity_root))
        if self.endpoint_secret_file is not None:
            args += ("--endpoint-secret-file", str(self.endpoint_secret_file))
        if self.token_policy_trust_file is not None:
            args += ("--token-policy-trust-file", str(self.token_policy_trust_file))
        if getattr(sys, "frozen", False):
            return (sys.executable, "--managed-node", *args)
        return (sys.executable, "-B", "-m", "mycelium_a13_macos.worker", *args)

    def _arm_deadline(self) -> None:
        """Arm the one wall deadline, after the handshake, from the owner clock.

        Two things are load-bearing.  It is armed only once the node is up, so
        the deadline can never fire inside ``start()`` and kill a node that has
        not executed anything yet.  And it is anchored to the budget owner's
        ``_started_at``, so a replacement stays inside the original allowance
        instead of minting a fresh one.  Enforcement lives in this timer thread,
        not in the guard: a blocked policy observer cannot defer it.
        """
        if self._started_at is None:
            raise WorkerError("worker_replacement_state_unavailable")
        remaining = (
            self._started_at
            + self.limits.max_wall_seconds
            - _CLEANUP_RESERVE_SECONDS
            - time.monotonic()
        )
        self._deadline_timer = threading.Timer(max(0, remaining), self._expire)
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def start(self) -> dict[str, Any]:
        if self._session is not None or self._closed.is_set():
            raise WorkerError("worker_restart_requires_new_instance")
        refusal = self.replacement_refusal()
        if refusal is not None:
            raise WorkerError(refusal)
        self._check()
        # A replacement transfers no authority: it shares the owner's execution
        # clock before it spawns, so it stays inside the original allowance.  The
        # owning worker's clock is set at handshake completion below, so its own
        # spawn+hello startup is never charged against the model's wall budget.
        if self._budget_owner is not self:
            self._started_at = self._budget_owner._started_at
            if self._started_at is None:
                raise WorkerError("worker_replacement_state_unavailable")
        argv = self._native_argv()
        _, response_timeout = _native_budgets(self.limits)
        with self._close_lock:
            if self._closed.is_set() or self._starting or self._session is not None:
                raise WorkerError("worker_restart_requires_new_instance")
            self._starting = True
        try:
            self._session = _ConcurrentNativeSession(
                argv=argv,
                node_id=self.node_id,
                run_id=self.run_id,
                deployment_id=self.deployment_id,
                timeout_seconds=response_timeout,
            )
            self.pid = self._session.pid
            self._birth_token = getattr(self._session, "birth_token", None)
            with self._close_lock:
                if self._closed.is_set():
                    raise WorkerError("worker_not_running")
            # Startup is not model execution: the handshake answers under its own
            # bounded allowance, never under the model's leftover wall budget.
            hello = self._session.send(
                command_id="managed-hello",
                command="hello",
                payload={},
                timeout_seconds=_STARTUP_TIMEOUT_SECONDS,
            )
            pid = self.pid
            if mycelium_host_platform.is_win32():
                group_owned = pid is not None and self._session.win32_group_owned()
            else:
                group_owned = pid is not None and os.getpgid(pid) == pid
            if hello.get("ok") is not True or not group_owned:
                raise WorkerError("worker_native_start_failed")
            self._group_owned = True
            self._started_at_utc_ms = int(time.time() * 1000)
            self._record_start_timing()
            if self._budget_owner is self:
                # One admitted execution owns the wall budget, and that budget
                # starts once the node is up.  Charging the spawn+hello against
                # it made a small ``max_wall_seconds`` impossible: 12 s minus the
                # 10 s cleanup reserve is a 2.0 s deadline, so the timer expired
                # inside ``start()`` -- killing the node before it executed
                # anything, and leaving a blocked-observer case no window at all
                # on the loaded host the L2 runs on.
                self._started_at = time.monotonic()
            self._arm_deadline()
            self._check()
            self._monitor = threading.Thread(
                target=self._watch, name="a13-worker-guard", daemon=True
            )
            self._monitor.start()
            return hello
        except BaseException as exc:
            with self._close_lock:
                self._cleanup_complete = False
                self._starting = False
                if self._session is None and getattr(exc, "code", None) == "node_process_cleanup_failed":
                    self._startup_cleanup_unproven = True
                    self._startup_cleanup_owner = getattr(exc, "cleanup_owner", None)
            self._record_start_failure(exc)
            self.close()
            raise
        finally:
            with self._close_lock:
                self._starting = False

    def _record_start_failure(self, error: BaseException) -> None:
        """Persist the closed cause of a failed native launch (diagnostics only).

        Run #10: the joined Air answered ``node_process_start_failed`` and kept
        no trace of which launch step failed.  ``NodeStartFailure`` is closed
        codes and small integers (phase, outcome, elapsed vs the launch budget,
        exception type, errno, launcher returncode) -- no path, message or new
        identifier -- written next to ``native-exit.private.json``.  It never
        replaces or masks the raised error.
        """
        failure = getattr(error, "start_failure", None)
        if self.capacity_root is None or failure is None:
            return
        try:
            from .authority import _atomic_private_json
            _atomic_private_json(self.capacity_root / "node-start-failure.private.json", {
                "protocol": "mycelium.a13.node_start_failure_diagnostic.v1",
                "run_id": self.run_id, "node_id": self.node_id,
                "recorded_at": int(time.time()),
                "error_code": getattr(error, "code", None),
                "public_code": getattr(error, "public_code", None),
                "cause": failure.as_record()})
        except Exception:  # noqa: BLE001 - a diagnostic must not change the outcome
            pass

    def _record_start_timing(self) -> None:
        """Record how much of the launch budget a SUCCESSFUL start used.

        The 2 s handshake budget is shared with the A-gate paths and is not
        loosened on a guess; the measured headroom on each Mac is the evidence
        a budget change would need.
        """
        timing = getattr(self._session, "launch_timing", None)
        if self.capacity_root is None or not isinstance(timing, Mapping):
            return
        try:
            marks = timing.get("phase_ms") or {}
            last = max((v for v in marks.values() if isinstance(v, int)), default=None)
            budget = timing.get("budget_ms")
            from .authority import _atomic_private_json
            _atomic_private_json(self.capacity_root / "node-start-timing.private.json", {
                "protocol": "mycelium.a13.node_start_timing_diagnostic.v1",
                "run_id": self.run_id, "node_id": self.node_id,
                "recorded_at": int(time.time()),
                "timing": dict(timing),
                "headroom_ms": (budget - last if isinstance(budget, int) and last is not None
                                else None)})
        except Exception:  # noqa: BLE001 - a diagnostic must not change the outcome
            pass

    def _reserve_command(self, command_id: str) -> Any:
        with self._account_lock:
            budget = getattr(self, "_budget_owner", self)
            if self._session is None or self._closed.is_set():
                raise WorkerError(_unavailable_code(self))
            if command_id in budget._commands or command_id in budget._pending_commands:
                raise WorkerError("worker_command_reconciliation_required")
            if (
                budget._command_count + len(budget._pending_commands)
                >= self.limits.max_commands
            ):
                raise WorkerError("worker_command_limit_exceeded")
            budget._pending_commands.add(command_id)
            return self._session

    def _retire_reservation(self, command_id: str) -> None:
        with self._account_lock:
            self._budget_owner._pending_commands.discard(command_id)

    def _commit_command(self, command_id: str, session: Any, *, command: str | None = None) -> None:
        with self._account_lock:
            budget = getattr(self, "_budget_owner", self)
            if (
                self._session is not session
                or self._closed.is_set()
                or command_id not in budget._pending_commands
            ):
                raise WorkerError(_unavailable_code(self))
            if budget._owner_stop_requested and command != "stop":
                raise WorkerError("worker_owner_stopped")
            budget._pending_commands.remove(command_id)
            budget._commands.add(command_id)
            budget._command_count += 1
            if command == "stop":
                # Delivery may succeed even when its acknowledgement is lost.
                budget._owner_stop_requested = True
            if command == "configure":
                self._configure_dispatched = True

    @staticmethod
    def _validate_response(
        response: Any, *, command_id: str, node_id: str
    ) -> dict[str, Any]:
        if not isinstance(response, Mapping):
            raise WorkerError("worker_native_response_invalid")
        detached = _detached_json_object(
            response, code="worker_native_response_invalid"
        )
        common = {"protocol", "command_id", "node_id", "ok", "route_ready"}
        expected = common | ({"result"} if detached.get("ok") is True else {"error"})
        if (
            set(detached) != expected
            or detached.get("protocol") != _NATIVE_PROTOCOL
            or detached.get("command_id") != command_id
            or detached.get("node_id") != node_id
            or detached.get("route_ready") is not False
            or type(detached.get("ok")) is not bool
        ):
            raise WorkerError("worker_native_response_invalid")
        if detached["ok"] is False:
            error = detached.get("error")
            if (
                not isinstance(error, dict)
                or set(error) != {"code"}
                or not isinstance(error.get("code"), str)
                or _OPERATION.fullmatch(error["code"]) is None
            ):
                raise WorkerError("worker_native_response_invalid")
        return detached

    def _authorize_exact(self, envelope: Mapping[str, Any]) -> None:
        if self._authorize_load is None:
            raise WorkerError("model_execution_not_authorized")
        try:
            authorized = self._authorize_load(_detached_json_object(envelope))
        except Exception:
            authorized = False
        if authorized is not True:
            raise WorkerError("model_execution_not_authorized")

    def _record_native_failure(self, session, *, command_id, command, response):
        """Bounded local diagnostics, never raw stderr or model/prompt values.

        Includes the native's own error code: it is the one field that names WHY the
        command failed, it is already validated against the closed operation grammar by
        ``_validate_response``, and it is re-validated by ``_native_error_code``.  Without
        it this record says only that a command failed on a node.
        """
        if self.capacity_root is None:return
        from .authority import _atomic_private_json
        document={'protocol':'mycelium.a13.native_failure_diagnostic.v1',
            'run_id':self.run_id,'node_id':self.node_id,'command_id':command_id,
            'command':command,**_bounded_native_diagnostics(getattr(session,'stderr',b''))}
        code=_native_error_code(response)
        if code is not None:document['error_code']=code
        _atomic_private_json(self.capacity_root/'native-failure.private.json',document)

    def _record_native_exit(self, session):
        """Bounded diagnostic for a worker guard trip.

        NOT necessarily a process exit: ``_watch`` calls ``_check()`` and the
        exit test inside one ``try``, and ``_check()`` raises for membership,
        resource-policy, storage, memory and wall-limit refusals.  Since
        ``self._session`` is never cleared, a null ``returncode`` with empty
        frames means ``_check()`` raised and nothing exited.

        A process exit destroys the only chance to learn why the worker is
        gone, and the generic follow-on code hides it entirely.  Record the
        bounded stderr-derived frames and exception classes so the cause stays
        inspectable.  This is local diagnostics only: it is not native success
        or failure evidence and never replaces the actual native result.
        """
        if self.capacity_root is None:return
        from .authority import _atomic_private_json
        # Name the commands this exit cost.  Measured 2026-09-19 (round p76r1,
        # preview.76): the peer's native exited with returncode 0 and an EMPTY stderr,
        # so every command it then claimed was answered `reconciliation_required`
        # within 70 ms of the claim, and the owner -- correctly refusing an unaccounted
        # command -- failed the deployment at startup qualification.  This record said
        # `worker_native_exited` and nothing connected the exit to a command, so the
        # cost had to be reconstructed from timestamps.  `_pending_commands` is exactly
        # the reserved-but-uncommitted set, so a non-empty one here means the native
        # died mid-command.  Bounded like every other diagnostic: the names are capped,
        # the count is honest, and a worker without the set records zero, never fails.
        try:
            in_flight=sorted(str(name) for name in (getattr(self,'_pending_commands',None) or ()))
        except TypeError:
            in_flight=[]
        _atomic_private_json(self.capacity_root/'native-exit.private.json',{
            'protocol':'mycelium.a13.native_exit_diagnostic.v1',
            'run_id':self.run_id,'node_id':self.node_id,
            'error_code':self._error,
            'error_detail':getattr(self,'_error_detail',None),
            'returncode':getattr(session,'returncode',None),
            'in_flight_count':len(in_flight),
            'in_flight_commands':in_flight[:3],
            **_bounded_native_diagnostics(getattr(session,'stderr',b''))})

    def _send(
        self,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
        deadline_monotonic_s: float | None,
    ) -> dict[str, Any]:
        if not isinstance(command, str) or command not in _ALLOWED:
            raise WorkerError("worker_command_not_allowed")
        if (
            not isinstance(command_id, str)
            or not _SEGMENT.fullmatch(command_id)
            or not isinstance(payload, Mapping)
        ):
            raise WorkerError("worker_command_invalid")
        if deadline_monotonic_s is not None and (
            not isinstance(deadline_monotonic_s, (int, float))
            or isinstance(deadline_monotonic_s, bool)
            or not math.isfinite(float(deadline_monotonic_s))
        ):
            raise WorkerError("worker_command_invalid")
        self._deadline_check(deadline_monotonic_s)
        payload_document = _detached_json_object(payload)
        _reject_execution_inputs(payload_document)
        if command == "install_token_path":
            from mycelium_node.token_provisioning import valid_policy_payload
            if not valid_policy_payload(payload_document):
                raise WorkerError("worker_command_invalid")
        if command == "bind_request_control" and "token_path" in payload_document:
            from mycelium_node.token_participants import valid_bind_token_shape
            if not valid_bind_token_shape(payload_document):
                raise WorkerError("worker_command_invalid")
        if command == "infer_start" and "token_grant" in payload_document:
            from mycelium_node.token_admission import valid_start_grant_shape
            if not valid_start_grant_shape(payload_document):
                raise WorkerError("worker_command_invalid")
        envelope = {
            "protocol": _NATIVE_PROTOCOL,
            "command_id": command_id,
            "run_id": self.run_id,
            "deployment_id": self.deployment_id,
            "command": command,
            "payload": payload_document,
        }
        with self._account_lock:
            if self._budget_owner._owner_stop_requested and command != "stop":
                raise WorkerError("worker_owner_stopped")
        session = self._reserve_command(command_id)
        committed = False
        try:
            if command != "stop":
                self._check(deadline_monotonic_s)
            self._deadline_check(deadline_monotonic_s)
            with self._account_lock:
                load_admitted = self._load_admitted
            if command in _REQUIRES_ADMITTED_LOAD and not load_admitted:
                raise WorkerError("model_load_not_admitted")
            if command in _EXACT_GRANT_COMMANDS:
                self._authorize_exact(envelope)
                self._deadline_check(deadline_monotonic_s)
                self._check(deadline_monotonic_s)
            if frozenset(payload_document) not in _OPERATION_PAYLOAD_FIELDS[command]:
                raise WorkerError("worker_command_invalid")
            self._deadline_check(deadline_monotonic_s)
            if self._token_policy is not None:
                self._token_policy.before(command, payload_document)
            self._commit_command(command_id, session, command=command)
            committed = True
            if deadline_monotonic_s is None:
                response = session.send(
                    command_id=command_id,
                    command=command,
                    payload=payload_document,
                )
            else:
                response = session.send_before(
                    command_id=command_id,
                    command=command,
                    payload=payload_document,
                    deadline_monotonic_s=float(deadline_monotonic_s),
                )
            validated = self._validate_response(
                response, command_id=command_id, node_id=self.node_id
            )
            if self._token_policy is not None:
                self._token_policy.after(command, payload_document, validated)
            if validated["ok"] is False and command in _EXACT_GRANT_COMMANDS:
                try:
                    self._record_native_failure(session,command_id=command_id,
                        command=command,response=validated)
                except Exception:
                    pass  # Diagnostic I/O must never replace the actual native failure.
            if command == "configure" and validated["ok"] is True:
                with self._account_lock:
                    if not self._closed.is_set() and self._session is session:
                        self._load_admitted = True
            return validated
        except BaseException as exc:
            if committed and getattr(session, "returncode", None) is not None:
                terminal_code = getattr(exc, "code", "worker_native_exited")
                with self._account_lock:
                    self._error = terminal_code
                self._mark_shutdown_actor("native_send_exit", terminal_code)
                self.close()
            raise
        finally:
            if not committed:
                self._retire_reservation(command_id)

    def send(
        self, *, command_id: str, command: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Send one closed native command without serializing its response wait."""

        return self._send(
            command_id=command_id,
            command=command,
            payload=payload,
            deadline_monotonic_s=None,
        )

    def send_before(
        self,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
        deadline_monotonic_s: float,
    ) -> dict[str, Any]:
        """Send within the caller's one absolute deadline; never restart it."""

        return self._send(
            command_id=command_id,
            command=command,
            payload=payload,
            deadline_monotonic_s=deadline_monotonic_s,
        )

    def open_progress(self, *, command_id: str, cursor: Any,
                      deadline_monotonic_s: float, capacity: int = 64):
        """Register independently pinned receipt verification before send.

        Registration grants no compute authority and uses the existing native
        reader; the caller owns closure and owner ACK forwarding.
        """
        self._check(deadline_monotonic_s)
        with self._account_lock:
            session = self._session
            if session is None or self._closed.is_set():
                raise WorkerError("worker_not_running")
        return session.open_progress(command_id=command_id, cursor=cursor,
            deadline_monotonic_s=deadline_monotonic_s, capacity=capacity)

    def close_progress(self, command_id: str, *, code: str = "token_progress_closed") -> None:
        with self._account_lock:
            session = self._session
        if session is not None:
            session.close_progress(command_id, code=code)

    def interrupt_command(self, command_id: str, *, code: str) -> bool:
        """Retire one native waiter; this does not undo an already-delivered effect."""

        if (
            not isinstance(command_id, str)
            or _SEGMENT.fullmatch(command_id) is None
            or not isinstance(code, str)
            or _OPERATION.fullmatch(code) is None
        ):
            raise WorkerError("worker_command_invalid")
        with self._account_lock:
            session = self._session
            if session is None or self._closed.is_set():
                raise WorkerError("worker_not_running")
        return bool(session.interrupt_command(command_id, code=code))

    def token_progress_cursor(self, command):
        if self._token_policy is None:
            raise WorkerError("token_path_activation_unprovisioned")
        return self._token_policy.cursor(command)

    def _expire(self) -> None:
        if not self._closed.is_set():
            self._error = "worker_wall_limit_exceeded"
            self._mark_shutdown_actor("expiry", "worker_wall_limit_exceeded")
            self.close()

    def _watch(self) -> None:
        while not self._closed.wait(0.25):
            try:
                self._check()
                if self._session is None or self._session.returncode is not None:
                    raise WorkerError("worker_native_exited")
            except WorkerError as error:
                terminal_code = str(error)
                self._error = terminal_code
                self._error_detail = getattr(error, "limit_detail", None)
                # Historical diagnostic tests bind _watch to a minimal guard stub;
                # real workers always own the accounting lock and actor snapshot.
                if hasattr(self, "_account_lock"):
                    self._mark_shutdown_actor(
                        "native_exit" if terminal_code == "worker_native_exited" else "guard",
                        terminal_code,
                    )
                # Capture the bounded cause BEFORE closing: after close the
                # stderr tail is the only surviving evidence of the exit.
                try: self._record_native_exit(self._session)
                except Exception: pass
                self.close()
                return
            except Exception:
                self._error = "worker_guard_failed"
                self._mark_shutdown_actor("guard", "worker_guard_failed")
                self.close()
                return

    def _mark_shutdown_actor(self, actor: str, error_code: str | None) -> None:
        """Keep the first terminal observation before cleanup changes returncode."""
        with self._account_lock:
            if self._shutdown_actor is None:
                self._shutdown_actor = actor
                self._shutdown_error_code = error_code
                session = self._session
                rc = getattr(session, "returncode", None)
                self._observed_returncode = rc if type(rc) is int else None

    def private_cause_snapshot(self) -> dict[str, Any]:
        """Closed, bounded process attribution for a cold-restart refusal."""
        with self._account_lock:
            code = self._shutdown_error_code if self._shutdown_actor is not None else self._error
            rc = (self._observed_returncode if self._shutdown_actor is not None
                  else getattr(self._session, "returncode", None))
            actor = self._shutdown_actor if self._shutdown_actor in {
                "guard", "expiry", "native_exit", "native_send_exit", "explicit_close"
            } else None
            return {
                "previous_error_code": code if isinstance(code, str) and code in _PRIVATE_CAUSE_CODES else None,
                "previous_shutdown_actor": actor or (
                    "native_exit_observed_without_guard" if type(rc) is int else None),
                "previous_returncode": rc if type(rc) is int else None,
                "previous_pid": self.pid if type(self.pid) is int and self.pid > 0 else None,
                "previous_birth_token": self._birth_token if isinstance(self._birth_token, str)
                    and re.fullmatch(r"[0-9]{1,20}(?::[0-9]{1,20})?", self._birth_token) else None,
                "previous_started_at_utc_ms": self._started_at_utc_ms
                    if type(self._started_at_utc_ms) is int and 0 < self._started_at_utc_ms < 10**15
                    else None,
            }

    def replacement_refusal(self) -> str | None:
        """Expired/stopped authority never mints another native process."""
        with self._account_lock:
            budget = self._budget_owner
            if budget._owner_stop_requested:
                return "worker_owner_stopped"
            if (budget._started_at is not None and time.monotonic() >=
                    budget._started_at + self.limits.max_wall_seconds - _CLEANUP_RESERVE_SECONDS):
                return "worker_wall_limit_exceeded"
            if budget._command_count + len(budget._pending_commands) >= self.limits.max_commands:
                return "worker_command_limit_exceeded"
            return None

    def inherit_execution_budget(self, previous: "ManagedWorker") -> None:
        """Transfer no authority: share the original accounting before start."""
        if (self._session is not None or self._started_at is not None
                or self._closed.is_set() or self.limits != previous.limits
                or (self.node_id, self.run_id, self.deployment_id) !=
                   (previous.node_id, previous.run_id, previous.deployment_id)):
            raise WorkerError("worker_replacement_state_unavailable")
        with previous._account_lock:
            refusal = previous.replacement_refusal()
            if refusal is not None:
                raise WorkerError(refusal)
            if previous._budget_owner._started_at is None:
                raise WorkerError("worker_replacement_state_unavailable")
            self._budget_owner = previous._budget_owner
            self._account_lock = previous._account_lock

    def requires_cold_restart(self) -> bool:
        """No model state, request KV, or old cleanup authority transfers."""
        with self._account_lock:
            return self._configure_dispatched or self._load_admitted

    def process_group_present(self) -> bool:
        if not self._group_owned or self.pid is None:
            return False
        if mycelium_host_platform.is_win32():
            session = self._session
            try:
                return session is not None and session.win32_group_owned()
            except Exception:  # noqa: BLE001 - an unobservable job is present
                return True
        try:
            os.killpg(self.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def status(self) -> dict[str, Any]:
        with self._account_lock:
            session = self._session
            running = session is not None and session.returncode is None
            return {
                "running": running,
                "stopping": self._closed.is_set() and running,
                "error_code": self._error,
                "model_load_authorized": self._load_admitted,
                "command_count": self._budget_owner._command_count,
                "resource_observation": self._last_observation,
                "route_ready": False,
            }

    def resident_bytes(self) -> int:
        """Resident bytes of this worker's process group while it runs, else 0 (numbers only)."""
        with self._account_lock:
            session = self._session
            running = session is not None and session.returncode is None
        return int(self._group_rss()) if running else 0

    def cleanup_proven(self) -> bool:
        """A pre-hello ownership refusal is not proof that no process survives."""
        with self._close_lock:
            return self._closed.is_set() and self._cleanup_complete and not self._starting

    def close(self) -> None:
        with self._close_lock:
            self._mark_shutdown_actor("explicit_close", None)
            with self._account_lock:
                self._closed.set()
            if self._deadline_timer is not None:
                self._deadline_timer.cancel()
            session = self._session or self._startup_cleanup_owner
            if session is not None:
                try:
                    session.close()
                except Exception:
                    self._cleanup_complete = False
                    self._error = self._error or "worker_cleanup_failed"
                else:
                    self._cleanup_complete = True
                    self._startup_cleanup_unproven = False
                    self._startup_cleanup_owner = None
            else:
                # An unassigned session does not prove its constructor never
                # spawned a child; a missing cleanup handle remains unknown.
                self._cleanup_complete = not self._startup_cleanup_unproven
            # PhysicalNodeProcess alone owns descriptor-, process-incarnation-,
            # and process-group-bound termination.  If it cannot prove safe
            # ownership, do not bypass that guard with an unverified killpg.
        if self._monitor is not None and self._monitor is not threading.current_thread():
            self._monitor.join(timeout=5)


def native_main() -> int:
    # PhysicalNodeProcess already launches this fixed entrypoint as a fresh
    # session/process-group leader.  Retain compatibility with direct packaged
    # invocation without attempting a second setsid() as an existing leader.
    # On Windows the node is contained by its owner's kill-on-close job object
    # instead of a session (mycelium_node.process.PhysicalNodeProcess).
    if not mycelium_host_platform.is_win32() and os.getsid(0) != os.getpid():
        os.setsid()
    from physical_inference_node import main

    return main()


if __name__ == "__main__":
    raise SystemExit(native_main())
