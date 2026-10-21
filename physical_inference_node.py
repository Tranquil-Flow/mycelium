#!/usr/bin/env python3.14
# SPDX-License-Identifier: AGPL-3.0-only
"""Command-bounded physical Mycelium Router/runtime node service.

Control uses canonical JSON lines on stdin/stdout. Diagnostics and native
sidecar logs remain on stderr. Every command is bound to one immutable run,
deployment, and node identity. Private signing and sidecar key material never
enters a serialization-facing object.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import fields, is_dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence
import uuid

try:  # POSIX-only; the win32 memory probe goes through mycelium_host_platform.
    import resource
except ImportError:  # pragma: no cover - exercised by the win32 import tests
    resource = None  # type: ignore[assignment]

import mycelium_deadlines
import mycelium_host_platform
from mycelium_iroh_sidecar import LoopbackEndpoint, SidecarClient
from mycelium_node.identity import load_node_signer
from mycelium_physical_runner.remote_probe import derive_local_run_scoped_identity
from mycelium_qualification.evidence import canonical_json_bytes, canonical_json_loads
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_router.contracts import (
    DeviceState,
    ExecutionGraph,
    LayerRange,
    PathCancellation,
    Placement,
    PlacementEdge,
    RequestContext,
    RouterConfig,
    Stage,
    StageCost,
)
from mycelium_router.serialization import path_manifest_from_dict
from mycelium_router.live_ports import (
    PublishedDeviceStateProvider,
    PublishedTopologyProvider,
)
from mycelium_router.adaptive_overlay import read_gate as _read_adaptive_gate
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.router import Router
from mycelium_router.transports.iroh import (
    DEFAULT_PREFILL_STALL_POLICY,
    IrohTransport,
    IrohTransportError,
    PeerBinding,
)
from mycelium_router.validation import validate_execution_graph, validate_manifest
from physical_sqlite_capacity import SQLiteQualificationCapacityPort

NODE_CONTROL_PROTOCOL = "mycelium.physical_node_control.v1"
NODE_OBSERVATION_PROTOCOL = "mycelium.physical_node_observation.v1"

# Declared upper bound on this node's per-request cleanup, published in the
# health observation's `interruptibility` block.
#
# It must fit inside the OWNER's cleanup fanout window, because that window is
# what the owner actually enforces: the owner own cancellation-and-cleanup budget is
# `mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS`, and the owner reserves
# `_CLEANUP_FALLBACK_RESERVE_SECONDS` for its receipt-recovery path, so the window
# this advertisement must fit is the registry s `route_cleanup_fanout_window_ms`
# (re-derived 2026-09-20 from the measured peer cleanup; ERRATA-20).  Read the
# registry instead of restating numbers here: these moved once already.
# The previous literal 2_000 ms was structurally unhonourable — no owner could
# ever wait that long, so a cleanup landing in the top of the declared range
# could only ever return `command_deadline_exceeded`, leaving
# `pending.cleanup_receipt` unset and blocking the terminal CAS. The remaining
# margin below the window covers command delivery plus receipt return.
#
# `tests/live/test_cleanup_bound_contract.py` asserts this constant still fits
# the owner's window, so the two numbers cannot drift apart again.
#
# The value itself is the deadline registry's published symbol
# (``mycelium_deadlines.PEER_ADVERTISED_MAXIMUM_TOTAL_CLEANUP_MS``, derived as
# window - transport margin - slack = 1500 - 200 - 100 = 1200 ms) assigned to
# this name rather than restated: this lane's measured 1200 ms and wp5-d9's
# derivation are one number with one owner, so the advertisement, the route's
# admission predicate and the registry cannot disagree.
MAXIMUM_TOTAL_CLEANUP_MS = mycelium_deadlines.PEER_ADVERTISED_MAXIMUM_TOTAL_CLEANUP_MS
CLEANUP_CONTROL_FRAME_PREFIX = b"MYCELIUM_CLEANUP_RESPONSE_V1 "
MAX_COMMAND_BYTES = 4 * 1024 * 1024
MAXIMUM_PHYSICAL_CLOCK_SKEW_SECONDS = 5.0
_MAX_IROH_DELIVERY_TIMEOUT_SECONDS = 240.0
_MAX_INFERENCE_COMPLETION_TIMEOUT_SECONDS = 240.0
# Upper bound of the legacy qualification path's wait for a remote cancellation
# receipt; below it the wait is 80% of the command budget, so one slow
# cross-host round trip does not fail startup qualification.
_MAX_LEGACY_TERMINAL_CLEANUP_SECONDS = 10.0
_CANCEL_RECEIPT_POLL_SECONDS = 0.05
# Reserve one complete parent snapshot attempt inside the immutable two-second
# owner budget.  If node-local teardown has not sealed by this handoff, the
# correlated command returns its non-authoritative pending observation and the
# route can perform exactly one fresh fallback proof without extending the
# deadline or racing duplicate work for the whole interval.
_CANCEL_RECEIPT_FALLBACK_RESERVE_SECONDS = 0.5
# Round-54 measurement need.  Seven physical runs put this node's
# ``infer_cancel_wait`` at 1.622-1.713 s against a transmitted
# ``deadline_budget_ms`` of 1499 -- a 91 ms spread, i.e. deadline-shaped rather
# than work-shaped -- while the transport cancellation itself measures
# 0.09-2.74 ms on its first attempt (``round54_cancel_roundtrip.py``) and the two
# retry loops provably return within one 50 ms poll of the moment their
# predicates hold (``round54_handler_phases.py``).  The unnamed span is therefore
# the three operations inside ``_apply_generation_fenced_cancellation``:
# ``runtime.cancel``, the transport fence, and ``router.cancel_local``.  They are
# timed into a bounded, payload-free append-only diagnostic so the next physical
# attempt can name the span instead of inferring it.
#
# Off unless an operator points the variable at a private path.  The diagnostic is
# deliberately NOT a new field on a validated command response and NOT a widening
# of an error contract: it never raises, writes at most 512 bytes per entry, and
# records no request id, path, task text or credential.
_CANCEL_PHASE_DIAGNOSTIC_ENV = "MYCELIUM_CANCEL_PHASE_DIAGNOSTIC_LOG"
_CANCEL_PHASE_DIAGNOSTIC_LINE_BYTES = 512


def _cancel_phase_diagnostic(
    phase: str,
    elapsed_ms: float,
    *,
    call_index: int,
    code: str = "",
) -> None:
    """Append one bounded, payload-free phase line; never raises, never blocks work."""

    try:
        destination = os.environ.get(_CANCEL_PHASE_DIAGNOSTIC_ENV)
        if not destination:
            return
        line = json.dumps(
            {
                "protocol": "mycelium.cancel_phase_diagnostic.v1",
                "phase": phase,
                "elapsed_ms": round(float(elapsed_ms), 3),
                "call_index": int(call_index),
                "code": str(code)[:96],
                "thread": threading.current_thread().name[:32],
            },
            sort_keys=True,
        )
        if len(line.encode("utf-8")) > _CANCEL_PHASE_DIAGNOSTIC_LINE_BYTES:
            return
        if not hasattr(os, "O_NOFOLLOW"):
            # No no-follow append on this host: skip the optional diagnostic
            # rather than follow a planted link.
            return
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, (line + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
    except BaseException:
        return
_COMMAND_FIELDS = frozenset(
    {"protocol", "command_id", "run_id", "deployment_id", "command", "payload"}
)
_REQUEST_CONTROL_FIELDS = frozenset(
    {
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
    }
)
_INFERENCE_COMMANDS = frozenset({"infer_start", "infer_decode"})


def _is_exact_owner_cancellation_successor(
    command_control: Mapping[str, Any],
    owner_control: object,
) -> bool:
    """Return whether owner cancellation monotonically overtook a command."""

    if not isinstance(owner_control, Mapping):
        return False
    immutable_fields = _REQUEST_CONTROL_FIELDS - {"cancellation_generation"}
    return bool(
        type(owner_control.get("cancellation_generation")) is int
        and type(command_control.get("cancellation_generation")) is int
        and owner_control["cancellation_generation"]
        > command_control["cancellation_generation"]
        and all(
            owner_control.get(field) == command_control.get(field)
            for field in immutable_fields
        )
    )


def _command_uses_inference_lane(document: object) -> bool:
    """Keep model execution out of the reserved cleanup/control lane."""

    return (
        isinstance(document, Mapping) and document.get("command") in _INFERENCE_COMMANDS
    )


def _command_uses_cleanup_response_priority(document: object) -> bool:
    """Return whether this response carries deadline-bound cleanup authority."""

    if not isinstance(document, Mapping):
        return False
    operation = document.get("command")
    if operation == "infer_cancel_wait":
        return True
    payload = document.get("payload")
    return bool(
        operation == "snapshot"
        and isinstance(payload, Mapping)
        and payload.get("receipt_only") is True
    )


def _owner_cancellation_interrupted_inference(
    service: object,
    document: object,
    error: BaseException,
) -> bool:
    """Recognize only the exact owner fence that overtook this command."""

    if not isinstance(error, IrohTransportError) or error.code != "path_cancelled":
        return False
    if not isinstance(document, Mapping):
        return False
    operation = document.get("command")
    payload = document.get("payload")
    if operation not in _INFERENCE_COMMANDS or not isinstance(payload, Mapping):
        return False
    if operation == "infer_start":
        request = payload.get("request")
        request_id = request.get("request_id") if isinstance(request, Mapping) else None
    else:
        request_id = payload.get("request_id")
    command_control = payload.get("control")
    if not isinstance(request_id, str) or not isinstance(command_control, Mapping):
        return False
    control_lock = getattr(service, "_control_lock", None)
    cancellation_controls = getattr(service, "_cancellation_controls", None)
    if control_lock is None or not isinstance(cancellation_controls, Mapping):
        return False
    with control_lock:
        owner_control = cancellation_controls.get(request_id)
        if not isinstance(owner_control, Mapping):
            # A cleanup snapshot retires transient cancellation-control state
            # as soon as exact runtime/transport absence is proved.  An
            # inference command already executing on another worker can then
            # unwind through its late ``path_cancelled`` exception.  The
            # bounded complete receipt is the monotonic successor authority;
            # use it to classify that already-fenced command instead of
            # turning a successful cancellation into request_failed_closed.
            receipts = getattr(service, "_request_cleanup_receipts", None)
            receipt = (
                receipts.get(request_id) if isinstance(receipts, Mapping) else None
            )
            if isinstance(receipt, Mapping) and receipt.get("complete") is True:
                owner_control = receipt
        if not isinstance(owner_control, Mapping):
            return False
        return _is_exact_owner_cancellation_successor(
            command_control,
            owner_control,
        )


def _age_cleanup_command_budget(
    document: object,
    *,
    queued_seconds: float,
) -> object:
    """Keep node-local receipt polling inside the frame's original budget."""

    if (
        not isinstance(document, Mapping)
        or document.get("command") != "infer_cancel_wait"
        or not isinstance(document.get("payload"), Mapping)
    ):
        return document
    payload = document["payload"]
    budget_ms = payload.get("deadline_budget_ms")
    if type(budget_ms) is not int or not (
        1 <= budget_ms <= mycelium_deadlines.COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS
    ):
        return document
    queued_ms = max(0, int(queued_seconds * 1_000.0))
    aged = dict(document)
    aged["payload"] = {
        **payload,
        # A command that reaches its reserved worker near expiry must still
        # apply the generation fence and take one exact snapshot, but it must
        # not start a new duration window after waiting in the stdin/executor
        # queue. The parent independently enforces the absolute owner deadline.
        "deadline_budget_ms": max(1, budget_ms - queued_ms),
    }
    return aged


class NodeCommandError(RuntimeError):
    """Fail-closed node command error carrying a stable public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _a10_execute_product_operation(runtime: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one A10 product verification operation on this node's runtime."""

    from mycelium_router.a10_product_verifier import execute_product_operation

    return execute_product_operation(runtime, payload)


#: A10 paired-benchmark batch policies (spec section 8): ``baseline`` is the
#: target-only runtime batch cap of one, ``candidate`` the configured cap.
A10_BATCH_POLICIES = ("baseline", "candidate")
A10_BATCH_POLICY_RECEIPT_PROTOCOL = "mycelium.a10_batch_policy_receipt.v1"
#: ``candidate@<ms>`` also sets the live hop collection window, in whole
#: milliseconds from 0 through this bound (the cross-host hop RTT is the guide).
A10_MAXIMUM_COLLECTION_WINDOW_MS = 250
_A10_CANDIDATE_WINDOW = re.compile(r"candidate@(0|[1-9][0-9]{0,2})\Z")


def parse_a10_batch_policy(policy: object) -> tuple[str, int | None]:
    """``(mode, window_ms)`` for one wire policy, or ``invalid_a10_batch_policy``.

    ``baseline`` and ``candidate`` keep the node's configured window;
    ``candidate@<ms>`` sets it.  Anything else, including a window above
    ``A10_MAXIMUM_COLLECTION_WINDOW_MS``, is refused.
    """

    if isinstance(policy, str) and policy in A10_BATCH_POLICIES:
        return policy, None
    match = _A10_CANDIDATE_WINDOW.fullmatch(policy) if isinstance(policy, str) else None
    _require(match is not None, "invalid_a10_batch_policy")
    window_ms = int(match.group(1))
    _require(window_ms <= A10_MAXIMUM_COLLECTION_WINDOW_MS, "invalid_a10_batch_policy")
    return "candidate", window_ms

# Router request states an ``inference_started`` observation may report.
_STARTED_REQUEST_STATUSES = frozenset({"DECODING", "COMPLETED", "CANCELLED"})


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise NodeCommandError(code)


_PREFILL_DIAGNOSTIC_CODE = re.compile(r"[a-z][a-z0-9_]{0,60}\Z")
_PREFILL_DIAGNOSTIC_COUNTERS = (
    "remote_frames_sent",
    "remote_frames_received",
    "router_frames_dispatched",
    "duplicate_frames",
)


def _emit_prefill_transport_diagnostic(transport: Any) -> None:
    """Print the transport's own bounded verdict for a stalled prefill hop.

    The wait that raises ``prefill_completion_timeout`` observes only
    ``transport.fatal_error``.  A hop that fails inside the transport's
    deferred forward path is recorded there as a scoped, per-path, *non-fatal*
    event, so that loop cannot see it and the operator is told the generic
    symptom instead of the real cause.  Emit the transport's local verdict in
    one fixed, allowlisted form on stderr so the managed worker's bounded
    diagnostic capture can recover it from the ``native-failure`` record.

    Diagnostics must never change control flow: every lookup is guarded and a
    failure to report is swallowed, so the raised code stays exactly
    ``prefill_completion_timeout``.  Only bounded counters and one allowlisted
    status token are printed -- never addresses, endpoint ids, keys, prompts,
    payloads or raw exception messages.
    """

    try:
        diagnostic = transport.prefill_hop_diagnostic()
    except BaseException:
        return
    if not isinstance(diagnostic, Mapping):
        return
    status = diagnostic.get("scoped_failure_code")
    if status is None:
        status = "none"
    if (
        not isinstance(status, str)
        or _PREFILL_DIAGNOSTIC_CODE.fullmatch(status) is None
    ):
        status = "none"
    fields = [f"scoped_failure_code={status}"]
    for name in _PREFILL_DIAGNOSTIC_COUNTERS:
        value = diagnostic.get(name)
        if type(value) is not int or value < 0 or value > 10**12:
            value = 0
        fields.append(f"{name}={value}")
    try:
        print(
            "MYCELIUM-TRANSPORT-DIAG " + " ".join(fields),
            file=sys.stderr,
            flush=True,
        )
    except BaseException:
        pass


_LOAD_PROOF_DIAGNOSTIC_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


# -- prefill wait on a measured link (A14 class D) -----------------------------
#
# The wait used to be a fixed ``min(command_timeout x 0.8, 240 s)`` that only
# watched ``transport.fatal_error``: a chunk that stopped crossing a degraded
# relay surfaced 240 s later as the generic ``prefill_completion_timeout``, and
# a long prompt on a slow but healthy link was refused by the same number.
# Every figure below comes from ``PrefillStallPolicy`` (the transport's own
# policy when it has one) and the transport's measured ``link_estimate``.

#: JSON envelope of one chunk hop frame on top of its activation payload
#: (graph / build header): used only until a real frame size is observed.
_PREFILL_FRAME_ENVELOPE_BYTES = 4096
#: Re-read the link estimate at least this often while nothing progresses.
_PREFILL_LINK_REFRESH_SECONDS = 1.0
_PREFILL_STALL_SOURCE = re.compile(r"[a-z][a-z0-9_:]{0,60}\Z")


_STDERR_LINE_LOCK = threading.Lock()


def _maximum_imported_lease_seconds(config: RouterConfig) -> float:
    """Longest peer-issued reservation lease this host mirrors.

    A peer issues leases scaled with its measured link
    (``PhysicalNodeService._kv_lease_window_seconds``), never longer than the
    stall policy's KV lease ceiling; that ceiling (plus the tolerated clock
    skew) still bounds how long a peer can hold this host's capacity.
    """

    return (
        DEFAULT_PREFILL_STALL_POLICY.kv_lease_ceiling_seconds(
            config.reservation_lease_seconds
        )
        + MAXIMUM_PHYSICAL_CLOCK_SKEW_SECONDS
    )


def _emit_stderr_line(text: str) -> None:
    """Write one whole diagnostic line; threads never interleave inside it."""

    with _STDERR_LINE_LOCK:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()


def _bounded_ms(seconds: float) -> int:
    value = int(round(max(0.0, float(seconds)) * 1000))
    return min(value, 10**12)


class _PrefillWaitBudget:
    """Deadlines of one prefill wait, re-derived as bytes complete.

    * total    ``clamp(remaining_bytes / goodput x 3 + chunks x crossings x 4
      x rtt_p95, 60 s, command_timeout x 0.8)`` from the last progress; the
      deadline only ever moves later and never past ``start + command_timeout
      x 0.8``;
    * stall    ``S = clamp(8 x rtt_p95 + chunk_bytes / goodput x 4, 30, 120)``;
    * watchdog ``W = (max_resends + 1) x S + 4 x rtt_p95`` of no progress.

    ``chunk_bytes`` is the largest observed frame of the request, or until
    one is observed ``chunk_tokens x hidden_size x activation_bytes + 4 KiB``;
    the total starts from the activation payload alone.  ``crossings`` is
    ``max(1, stages - 1)``: every stage boundary may be a remote hop.
    """

    def __init__(
        self,
        *,
        transport: Any,
        request_id: str,
        prompt_tokens: int,
        chunk_size_tokens: int,
        graph: Any,
        command_timeout: float,
    ) -> None:
        from mycelium_router.transports import iroh as iroh_transport_module

        self.transport = transport
        self.request_id = request_id
        policy = getattr(transport, "stall_policy", None)
        if policy is None:
            policy = iroh_transport_module.DEFAULT_PREFILL_STALL_POLICY
        self.policy = policy
        tokens = max(1, int(prompt_tokens))
        chunk_tokens = (
            tokens
            if chunk_size_tokens <= 0 or chunk_size_tokens >= tokens
            else int(chunk_size_tokens)
        )
        self.chunks = -(-tokens // chunk_tokens)
        hidden = getattr(graph, "hidden_size", 0)
        width = getattr(graph, "activation_bytes", 0)
        payload = (
            chunk_tokens * hidden * width
            if type(hidden) is int and type(width) is int and hidden > 0 and width > 0
            else 0
        )
        self.chunk_payload_bytes = payload
        stages = getattr(graph, "stages", ()) or ()
        try:
            stage_count = len(stages)
        except TypeError:
            stage_count = 0
        self.crossings = max(1, stage_count - 1)
        self.command_timeout = float(command_timeout)
        self.started = time.monotonic()
        self.hard_deadline = self.started + self.command_timeout * float(
            policy.total_command_fraction
        )
        self.progress_source = callable(getattr(transport, "prefill_progress", None))
        self.link = self._read_link()
        self.link_read_at = self.started
        self.progress_events = -1
        self.last_progress_at = self.started
        self.progress: Mapping[str, Any] = {}
        self.deadline = self.started + policy.total_seconds(
            remaining_bytes=self.chunks * self.crossings * payload,
            chunks=self.chunks * self.crossings,
            link=self.link,
            command_timeout=self.command_timeout,
        )
        self._register()

    def _read_link(self) -> Any:
        reader = getattr(self.transport, "link_estimate", None)
        if callable(reader):
            try:
                link = reader(None)
                if (
                    float(link.goodput_bytes_per_second) > 0
                    and float(link.rtt_p95_seconds) > 0
                ):
                    return link
            except Exception:
                pass
        return SimpleNamespace(
            goodput_bytes_per_second=float(
                self.policy.unmeasured_goodput_bytes_per_second
            ),
            rtt_p95_seconds=float(self.policy.unmeasured_rtt_seconds),
            goodput_source="unmeasured",
            rtt_source="unmeasured",
        )

    def _register(self) -> None:
        watch = getattr(self.transport, "watch_prefill", None)
        if callable(watch):
            watch(self.request_id, deadline_monotonic=self.deadline)

    def release(self) -> None:
        release = getattr(self.transport, "release_prefill_watch", None)
        if callable(release):
            try:
                release(self.request_id)
            except Exception:
                pass

    def chunk_bytes(self) -> int:
        observed = self.progress.get("max_frame_bytes", 0) if self.progress else 0
        if type(observed) is not int:
            observed = 0
        estimate = (
            self.chunk_payload_bytes + _PREFILL_FRAME_ENVELOPE_BYTES
            if self.chunk_payload_bytes
            else 0
        )
        return max(observed, estimate)

    def stall_seconds(self) -> float:
        return self.policy.stall_seconds(self.chunk_bytes(), self.link)

    def watchdog_seconds(self) -> float:
        return self.policy.watchdog_seconds(self.chunk_bytes(), self.link)

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_progress_at

    def observe(self) -> str | None:
        """Refresh progress; return the transport's verdict code, if any."""

        if not self.progress_source:
            return None
        now = time.monotonic()
        try:
            progress = self.transport.prefill_progress(self.request_id)
        except Exception:
            progress = None
        if not isinstance(progress, Mapping):
            return None
        self.progress = progress
        events = progress.get("progress_events")
        if type(events) is int and events != self.progress_events:
            self.progress_events = events
            last = progress.get("last_progress_at")
            if isinstance(last, (int, float)) and not isinstance(last, bool):
                self.last_progress_at = max(self.started, float(last))
            self.link = self._read_link()
            self.link_read_at = now
            if events > 0:
                self._extend()
        elif now - self.link_read_at >= _PREFILL_LINK_REFRESH_SECONDS:
            self.link = self._read_link()
            self.link_read_at = now
        code = progress.get("failure_code")
        if isinstance(code, str) and _PREFILL_DIAGNOSTIC_CODE.fullmatch(code):
            return code
        return None

    def _extend(self) -> None:
        chunk = max(1, self.chunk_bytes())
        total = self.chunks * self.crossings * chunk
        confirmed = self.progress.get("bytes_confirmed", 0)
        if type(confirmed) is not int or confirmed < 0:
            confirmed = 0
        remaining = max(0, total - confirmed)
        candidate = self.last_progress_at + self.policy.total_seconds(
            remaining_bytes=remaining,
            chunks=-(-remaining // chunk),
            link=self.link,
            command_timeout=self.command_timeout,
        )
        extended = min(self.hard_deadline, max(self.deadline, candidate))
        if extended > self.deadline:
            self.deadline = extended
            self._register()

    def stalled(self) -> bool:
        return self.progress_source and self.idle_seconds() >= self.watchdog_seconds()

    def expiry_code(self) -> str:
        if self.progress_source and self.idle_seconds() >= self.stall_seconds():
            return "prefill_transport_stalled"
        return "prefill_completion_timeout"

    def snapshot(self) -> dict[str, Any]:
        """The wait's figures for an evidence bundle (bounded ints only)."""

        snapshot: dict[str, Any] = {
            "idle_ms": _bounded_ms(self.idle_seconds()),
            "elapsed_ms": _bounded_ms(time.monotonic() - self.started),
            "deadline_ms": _bounded_ms(self.deadline - self.started),
            "stall_window_ms": _bounded_ms(self.stall_seconds()),
            "watchdog_ms": _bounded_ms(self.watchdog_seconds()),
            "chunks": int(self.chunks),
            "chunk_bytes": int(self.chunk_bytes()),
        }
        for name in (
            "progress_events",
            "bytes_confirmed",
            "max_frame_bytes",
            "stall_resends",
            "send_lock_stalls",
        ):
            value = self.progress.get(name, 0) if self.progress else 0
            snapshot[name] = value if type(value) is int and 0 <= value <= 10**12 else 0
        code = self.progress.get("failure_code") if self.progress else None
        snapshot["failure_code"] = (
            code if isinstance(code, str) and _PREFILL_DIAGNOSTIC_CODE.fullmatch(code) else None
        )
        return snapshot

    def emit_diagnostic(self, code: str) -> None:
        """One allowlisted stderr line; never changes control flow."""

        try:
            link = self.link
            fields = {
                "code": code if _PREFILL_DIAGNOSTIC_CODE.fullmatch(code) else "none",
                "idle_ms": _bounded_ms(self.idle_seconds()),
                "elapsed_ms": _bounded_ms(time.monotonic() - self.started),
                "deadline_ms": _bounded_ms(self.deadline - self.started),
                "stall_window_ms": _bounded_ms(self.stall_seconds()),
                "watchdog_ms": _bounded_ms(self.watchdog_seconds()),
                "chunks": int(self.chunks),
                "chunk_bytes": int(self.chunk_bytes()),
            }
            for name in (
                "progress_events",
                "bytes_confirmed",
                "stall_resends",
                "send_lock_stalls",
            ):
                value = self.progress.get(name, 0) if self.progress else 0
                fields[name] = (
                    value if type(value) is int and 0 <= value <= 10**12 else 0
                )
            fields["goodput_Bps"] = int(min(float(link.goodput_bytes_per_second), 1e12))
            fields["rtt_p95_ms"] = _bounded_ms(float(link.rtt_p95_seconds))
            for name in ("goodput_source", "rtt_source"):
                value = getattr(link, name, "unmeasured")
                fields[name] = (
                    value
                    if isinstance(value, str) and _PREFILL_STALL_SOURCE.fullmatch(value)
                    else "unknown"
                )
            _emit_stderr_line(
                "MYCELIUM-PREFILL-STALL-DIAG "
                + " ".join(f"{key}={value}" for key, value in fields.items())
            )
        except BaseException:
            pass


def _print_transport_stall_event(event: str, fields: Mapping[str, Any]) -> None:
    """Stall-recovery observer: a bounded stderr line in the node's own tail.

    The route prints every node's stderr tail when startup qualification
    fails, so a *peer's* recovery attempts become visible there too.
    """

    try:
        if _PREFILL_DIAGNOSTIC_CODE.fullmatch(event) is None:
            return
        parts = [f"event={event}"]
        for name in ("attempts", "stall_window_ms"):
            value = fields.get(name)
            if type(value) is int and 0 <= value <= 10**12:
                parts.append(f"{name}={value}")
        # A send-lock stall names the frame kind holding the shared data
        # client (a message type name) and how long it has held it.
        holder_kind = fields.get("holder_kind")
        holder_age = fields.get("holder_age_ms")
        if (
            isinstance(holder_kind, str)
            and _EVIDENCE_FRAME_KIND.fullmatch(holder_kind)
            and type(holder_age) is int
            and 0 <= holder_age <= 10**12
        ):
            parts.append(f"holder_kind={holder_kind}")
            parts.append(f"holder_age_ms={holder_age}")
        _emit_stderr_line("MYCELIUM-TRANSPORT-STALL " + " ".join(parts))
    except BaseException:
        pass


# -- FailureReport diagnostics (A14 serve-2, f9a8d961 r1) ----------------------
#
# r1's final node sent a FailureReport mid-prefill and the entry failed
# ``route_qualify`` with ``inference_start_failed``; the report's reason was in
# neither node's stderr nor bundle.  Both ends now print it, and the entry
# names it when a start fails.

_FAILURE_REPORT_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,95}\Z")


def _failure_report_token(value: Any) -> str | None:
    if type(value) is int and -(2**31) <= value < 2**31:
        return str(value)
    if not isinstance(value, str) or not value:
        return None
    if _FAILURE_REPORT_VALUE.fullmatch(value):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def _failure_report_parts(fields: Mapping[str, Any], names: Sequence[tuple[str, str]]) -> list[str]:
    parts: list[str] = []
    for key, label in names:
        token = _failure_report_token(fields.get(key))
        if token is not None:
            parts.append(f"{label}={token}")
    return parts


def _print_failure_report_event(
    direction: str, fields: Mapping[str, Any], *, request_id: Any = None
) -> None:
    """Transport observer: one bounded line per FailureReport sent or received.

    ``reason`` is the reporter's failure code (e.g. ``kv_lease_expired``),
    ``placement`` the hop that failed, ``token_index`` its chunk (``-1`` = the
    progressive first chunk) or decode step.  No request or path id.
    """

    del request_id
    try:
        if direction not in {"sent", "received"}:
            return
        parts = [f"direction={direction}"] + _failure_report_parts(
            fields,
            (
                ("peer", "peer"),
                ("scope", "scope"),
                ("reason", "reason"),
                ("placement_id", "placement"),
                ("token_index", "token_index"),
                ("node_id", "node"),
                ("edge_id", "edge"),
            ),
        )
        _emit_stderr_line("MYCELIUM-FAILURE-REPORT " + " ".join(parts))
    except BaseException:
        pass


def _print_inference_start_failure(status: str, cause: Mapping[str, Any] | None) -> None:
    """Name why ``infer_start`` failed, just before ``inference_start_failed``.

    The control error carries only the code, so the route's cause chain gets
    this line through the node's stderr tail (``node_stderr_tail``).
    """

    try:
        parts = [f"status={_failure_report_token(status) or 'unknown'}"]
        if not cause:
            parts.append("cause=none")
        else:
            parts.append("cause=failure_report")
            parts += _failure_report_parts(
                cause,
                (
                    ("source_node_id", "source"),
                    ("scope", "scope"),
                    ("reason", "reason"),
                    ("stage_id", "stage"),
                    ("placement_id", "placement"),
                    ("token_index", "token_index"),
                    ("path_attempt", "path_attempt"),
                ),
            )
            parts.append(f"applied={1 if cause.get('applied') else 0}")
        _emit_stderr_line("MYCELIUM-INFERENCE-START-FAILED " + " ".join(parts))
    except BaseException:
        pass


# -- evidence bundle (A14 class D, round 2) -------------------------------------
#
# A failed startup tears the nodes down; their stderr lived only in the
# parent's 16 KiB pipe ring and no thread dump existed.  A node therefore keeps
# its own stderr ring and, on a prefill stall of its own or when the route asks
# (``diagnostic_evidence``), writes one bounded, privacy-reduced bundle *beside*
# its staging root: ``<staging_root>.evidence/`` is touched neither by the
# controller's remote cleanup (which removes exactly the staging root) nor by
# the next staging.

_EVIDENCE_PROTOCOL = "mycelium.node_evidence_bundle.v1"
#: Bytes of this process's own stderr kept for a bundle (most recent).
_STDERR_RING_BYTES = 256 * 1024
#: One bundle file / one ``diagnostic_evidence`` result at most (the control
#: channel carries frames of up to 1 MiB).
_EVIDENCE_BUNDLE_MAX_BYTES = 768 * 1024
#: Bundles kept per node; older ones are pruned.
_EVIDENCE_BUNDLES_KEPT = 8
_EVIDENCE_STACK_FRAMES = 48
_EVIDENCE_STACK_BYTES = 192 * 1024
_EVIDENCE_FRAME_KIND = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")

_REDACT_HOME = re.compile(r"/(Users|home)/[^/\s\"']+")
_REDACT_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")
_REDACT_IPV6 = re.compile(
    r"(?i)(?<![\w:])(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}(?![\w:])"
    r"|(?<![\w:])[0-9a-f]{0,4}(?::[0-9a-f]{1,4})*::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?(?![\w:])"
)
_REDACT_HEX = re.compile(r"(?i)(?<![0-9a-z])[0-9a-f]{16,}(?![0-9a-z])")
# Endpoint ids (z-base-32), base64 keys: long runs without "_" (so long
# Python identifiers in stacks stay readable).
_REDACT_TOKEN = re.compile(r"(?<![0-9A-Za-z+/=_-])[0-9A-Za-z+/=-]{40,}(?![0-9A-Za-z+/=_-])")
_EVIDENCE_SEQUENCE = iter(range(1, 1 << 62))
_EVIDENCE_SEQUENCE_LOCK = threading.Lock()
_REDACT_FRAME_LINE = re.compile(r'\s+File "[^"\n]*", line \d+, in [\w<>.]+\Z')
_REDACT_EXCEPTION_LINE = re.compile(r"([A-Za-z_][\w.]{0,200})(?::\s?(.*))?\Z")
_REDACT_KEPT_PREFIXES = (
    "MYCELIUM-",
    "Traceback (most recent call last):",
    "physical-node ",
    "During handling of the above exception",
    "The above exception was the direct cause",
)


def _scrub_identifiers(line: str) -> str:
    line = _REDACT_HOME.sub(lambda match: f"/{match.group(1)}/<user>", line)
    line = _REDACT_IPV6.sub("<ipv6>", line)
    line = _REDACT_IPV4.sub("<ipv4>", line)
    line = _REDACT_HEX.sub("<hex>", line)
    return _REDACT_TOKEN.sub("<id>", line)


def redact_evidence_text(text: str) -> str:
    """Privacy-reduce process output for an evidence bundle.

    The same allowlist idea as ``MYCELIUM-SIDECAR-EVENT`` forwarding: only
    output whose shape the product controls is kept - its own ``MYCELIUM-*``
    diagnostic lines (built from bounded fields), fixed node lines, and
    Python traceback structure (frame lines, the source lines under them and
    the exception *type*).  An exception message and any other line are
    replaced by their length.  Every kept line is then scrubbed of anything
    shaped like an endpoint id, key, digest, IP address or home directory.
    """

    out: list[str] = []
    previous = ""
    for line in str(text).splitlines():
        kept: str | None = None
        kind = "other"
        if line.startswith(_REDACT_KEPT_PREFIXES):
            kept, kind = line, "fixed"
        elif _REDACT_FRAME_LINE.fullmatch(line):
            kept, kind = line, "frame"
        elif line[:1].isspace() and previous in {"frame", "source"}:
            kept, kind = line, "source"
        else:
            match = _REDACT_EXCEPTION_LINE.fullmatch(line)
            if match is not None and line.strip():
                message = match.group(2)
                kept = match.group(1) + (
                    "" if message is None else f": <redacted {len(message)} chars>"
                )
                kind = "exception"
        if kept is None:
            out.append(f"<redacted line {len(line)} chars>")
        else:
            out.append(_scrub_identifiers(kept))
        previous = kind
    return "\n".join(out) + ("\n" if out else "")


class _StderrRing:
    """The most recent bytes this process wrote to its stderr (bounded)."""

    def __init__(self, limit_bytes: int = _STDERR_RING_BYTES) -> None:
        self.limit_bytes = int(limit_bytes)
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self.total_bytes = 0

    def append(self, text: str) -> None:
        data = text.encode("utf-8", errors="replace")
        with self._lock:
            self.total_bytes += len(data)
            self._buffer.extend(data)
            excess = len(self._buffer) - self.limit_bytes
            if excess > 0:
                del self._buffer[:excess]

    def text(self) -> str:
        with self._lock:
            data = bytes(self._buffer)
        return data.decode("utf-8", errors="replace")


class _StderrTee:
    """``sys.stderr`` replacement that also feeds the ring."""

    def __init__(self, stream: Any, ring: _StderrRing) -> None:
        self._stream = stream
        self._ring = ring

    def write(self, text: str) -> int:
        try:
            self._ring.append(text)
        except Exception:
            pass
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


_STDERR_RING: _StderrRing | None = None


def _install_stderr_ring(limit_bytes: int = _STDERR_RING_BYTES) -> _StderrRing:
    """Tee this process's ``sys.stderr`` into a bounded ring; return the ring."""

    global _STDERR_RING
    ring = _StderrRing(limit_bytes)
    sys.stderr = _StderrTee(sys.stderr, ring)
    _STDERR_RING = ring
    return ring


def _thread_stacks() -> list[dict[str, Any]]:
    """All threads' Python stacks (file, line, function, source line).

    No local variable values are captured: a stack names code, never data.
    """

    frames = sys._current_frames()
    threads = {thread.ident: thread for thread in threading.enumerate()}
    stacks: list[dict[str, Any]] = []
    budget = _EVIDENCE_STACK_BYTES
    # Capture the emitting thread before other workers consume the fixed byte
    # budget. Thread IDs have no diagnostic priority and may sort ahead of it.
    current_ident = threading.get_ident()
    for ident, frame in sorted(
        frames.items(), key=lambda item: (item[0] != current_ident, str(item[0]))
    ):
        thread = threads.get(ident)
        lines: list[str] = []
        for entry in traceback.format_stack(frame)[-_EVIDENCE_STACK_FRAMES:]:
            lines.extend(entry.rstrip("\n").splitlines())
        lines = [_scrub_identifiers(line) for line in lines]
        size = sum(len(line) + 1 for line in lines)
        if size > budget:
            lines = lines[-max(1, budget // 160):]
            size = sum(len(line) + 1 for line in lines)
        budget = max(0, budget - size)
        stacks.append(
            {
                "name": "unknown" if thread is None else thread.name[:96],
                "daemon": None if thread is None else bool(thread.daemon),
                "stack": lines,
            }
        )
    return stacks


def _safe_evidence_token(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]", "_", str(value))[:64]
    return text or "none"


def _emit_load_proof_diagnostic(
    proof: Mapping[str, Any], *, expected_load_proof_digest: Any
) -> None:
    """Name which part of a refused placement load proof differed.

    ``placement_load_proof_mismatch`` alone does not say whether the weights,
    the probe, the runtime identity or the stage pack differed (two-Mac run 14
    had to reload the stage offline to learn it was the probe).  Print one
    fixed-shape line of ``sha256:`` component digests of the proof this node
    observed; the managed worker keeps only those allowlisted values, and the
    operator diffs them against the compiled ``node-N-load-proof.json``.
    Never changes control flow: failures to report are swallowed.
    """

    def digest(value: Any) -> str:
        return value if isinstance(value, str) and _LOAD_PROOF_DIAGNOSTIC_DIGEST.fullmatch(value) else "none"

    try:
        fields = {
            "expected_load_proof_digest": digest(expected_load_proof_digest),
            "observed_load_proof_digest": layer_load_proof_digest(proof),
            "loaded_tensor_digest": digest(proof.get("loaded_tensor_digest")),
            "probe_digest": digest(proof.get("probe_digest")),
            "runtime_identity_digest": "sha256:"
            + hashlib.sha256(canonical_json_bytes(proof.get("runtime_identity"))).hexdigest(),
            "stage_pack_digest": digest(proof.get("stage_pack_digest")),
        }
        print(
            "MYCELIUM-LOAD-PROOF-DIAG "
            + " ".join(f"{key}={value}" for key, value in fields.items()),
            file=sys.stderr,
            flush=True,
        )
    except BaseException:
        pass


def _validated_endpoint_secret_file(value: Path | None) -> Path | None:
    if value is None:
        return None
    _require(isinstance(value, Path), "invalid_endpoint_secret_file")
    lexical = os.fspath(value)
    _require(
        bool(lexical)
        and value.is_absolute()
        and not any(
            ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F
            for character in lexical
        ),
        "invalid_endpoint_secret_file",
    )
    return value


def _exact_fields(
    document: Any, expected: set[str] | frozenset[str], code: str
) -> dict[str, Any]:
    _require(isinstance(document, dict) and set(document) == set(expected), code)
    return document


def _plain_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain_json(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise NodeCommandError("nonserializable_observation")


def _bounded_command_output(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > 64 * 1024:
        return None
    return completed.stdout


def _host_available_memory_bytes() -> int:
    if mycelium_host_platform.is_win32():
        return mycelium_host_platform.available_memory_bytes()
    if sys.platform.startswith("linux"):
        try:
            values = {
                key.rstrip(":"): int(value) * 1024
                for line in Path("/proc/meminfo")
                .read_text(encoding="utf-8")
                .splitlines()
                if len(parts := line.split()) >= 2
                for key, value in [parts[:2]]
                if value.isdigit()
            }
        except OSError:
            values = {}
        if values.get("MemAvailable", 0) > 0:
            return values["MemAvailable"]
    if sys.platform == "darwin":
        output = _bounded_command_output(["/usr/bin/vm_stat"])
        if output:
            page_match = re.search(r"page size of (\d+) bytes", output)
            page_size = int(page_match.group(1)) if page_match else 4096
            pages = 0
            for label in (
                "Pages free",
                "Pages inactive",
                "Pages speculative",
                "Pages purgeable",
            ):
                match = re.search(
                    rf"^{re.escape(label)}:\s+(\d+)\.", output, re.MULTILINE
                )
                if match:
                    pages += int(match.group(1))
            if pages > 0:
                return pages * page_size
    try:
        return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError):
        return 0


def _process_rss_bytes() -> int:
    if mycelium_host_platform.is_win32():
        return mycelium_host_platform.process_resident_bytes()
    if sys.platform.startswith("linux"):
        try:
            resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError, IndexError):
            pass
    if sys.platform == "darwin":
        output = _bounded_command_output(
            ["/bin/ps", "-o", "rss=", "-p", str(os.getpid())]
        )
        if output and output.strip().isdigit():
            return int(output.strip()) * 1024
    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maximum if sys.platform == "darwin" else maximum * 1024)


def _host_swap_used_bytes() -> int:
    if sys.platform.startswith("linux"):
        try:
            values = {
                key.rstrip(":"): int(value) * 1024
                for line in Path("/proc/meminfo")
                .read_text(encoding="utf-8")
                .splitlines()
                if len(parts := line.split()) >= 2
                for key, value in [parts[:2]]
                if value.isdigit()
            }
        except OSError:
            return 0
        return max(0, values.get("SwapTotal", 0) - values.get("SwapFree", 0))
    if sys.platform == "darwin":
        output = _bounded_command_output(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
        if output:
            match = re.search(r"used = ([0-9.]+)([MGT])", output)
            if match:
                multiplier = {"M": 2**20, "G": 2**30, "T": 2**40}[match.group(2)]
                return int(float(match.group(1)) * multiplier)
    return 0


def _host_thermal_state() -> str | None:
    if sys.platform.startswith("linux"):
        temperatures: list[int] = []
        for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
            try:
                temperatures.append(int(path.read_text(encoding="utf-8").strip()))
            except (OSError, ValueError):
                continue
        if temperatures:
            peak = max(temperatures)
            if peak >= 95_000:
                return "critical"
            if peak >= 85_000:
                return "serious"
            return "nominal"
    return None


def _host_power_state() -> str | None:
    if sys.platform == "darwin":
        output = _bounded_command_output(["/usr/bin/pmset", "-g", "batt"])
        if output:
            charge = re.search(r"(\d+)%", output)
            if "AC Power" in output:
                return "external"
            if charge and int(charge.group(1)) <= 5:
                return "critical_battery"
            return "battery"
    if sys.platform.startswith("linux"):
        supplies = sorted(Path("/sys/class/power_supply").glob("BAT*"))
        if supplies:
            try:
                capacity = int((supplies[0] / "capacity").read_text().strip())
                status = (supplies[0] / "status").read_text().strip().lower()
            except (OSError, ValueError):
                return None
            if status in {"charging", "full"}:
                return "external"
            return "critical_battery" if capacity <= 5 else "battery"
    return None


def _runtime_build_digest(backend: str) -> str:
    names = [
        "physical_inference_node.py",
        "runtime_loader.py",
        "model_adapters.py",
        "numpy_runtime.py",
        "mycelium_router/mlx_runtime.py"
        if backend == "mlx"
        else "mycelium_router/numpy_runtime.py",
    ]
    digest = hashlib.sha256()
    digest.update(sys.version.encode("utf-8"))
    # PyInstaller imports the node from its PYZ, but ships canonical source as
    # a13-runtime-source data; __file__'s sibling is not a shipped .py file.
    source_root = (Path(sys._MEIPASS) / "a13-runtime-source"
                   if getattr(sys, "frozen", False) else Path(__file__).resolve().parent)
    for name in names:
        path = source_root / name
        digest.update(name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            raise NodeCommandError("runtime_build_source_unavailable") from exc
    return "sha256:" + digest.hexdigest()


def _route_decode_mode(graph: ExecutionGraph, architecture: str | None) -> str:
    """Choose one decode mode supported by every active route placement."""

    route_backends = {
        placement.runtime_backend
        for stage in graph.stages
        for placement in stage.placements
        if placement.lifecycle_state == "ACTIVE"
    }
    if not route_backends or route_backends - {"mlx", "numpy", "pixel-stdlib", "mobile-rust-qwen2"}:
        raise NodeCommandError("unsupported_runtime_backend")
    if route_backends & {"pixel-stdlib", "mobile-rust-qwen2"}:
        return "complete_context_replay"
    if "numpy" in route_backends and architecture not in {"qwen2", "qwen3"}:
        return "complete_context_replay"
    return "stage_local_kv"


def execution_graph_from_document(document: Any) -> ExecutionGraph:
    root_fields = {
        "deployment_id",
        "deployment_epoch",
        "topology_version",
        "model_id",
        "resolved_commit",
        "manifest_digest",
        "entry_stage_id",
        "final_stage_id",
        "hidden_size",
        "activation_bytes",
        "token_envelope_bytes",
        "stages",
        "edges",
        "loopback_edges",
        "protocol",
    }
    root = _exact_fields(document, root_fields, "invalid_execution_graph_fields")
    _require(
        root["protocol"] == "mycelium.execution_graph.v1",
        "invalid_execution_graph_protocol",
    )
    _require(isinstance(root["stages"], list), "invalid_execution_graph_stages")
    stages: list[Stage] = []
    for stage_document in root["stages"]:
        stage_data = _exact_fields(
            stage_document,
            {"stage_id", "layer_range", "component_roles", "stage_cost", "placements"},
            "invalid_stage_fields",
        )
        layer = _exact_fields(
            stage_data["layer_range"],
            {"start_layer", "end_layer_exclusive", "layer_count"},
            "invalid_layer_range_fields",
        )
        cost = _exact_fields(
            stage_data["stage_cost"],
            {
                "prefill_work_units_per_prompt_token",
                "decode_work_units_per_token",
                "kv_bytes_per_context_token",
            },
            "invalid_stage_cost_fields",
        )
        _require(
            isinstance(stage_data["component_roles"], list), "invalid_component_roles"
        )
        _require(isinstance(stage_data["placements"], list), "invalid_placements")
        placements: list[Placement] = []
        for placement_document in stage_data["placements"]:
            placement_data = _exact_fields(
                placement_document,
                {
                    "placement_id",
                    "node_id",
                    "replica_group_id",
                    "assignment_id",
                    "stage_signature",
                    "load_proof_digest",
                    "runtime_backend",
                    "runtime_endpoint",
                    "lifecycle_state",
                },
                "invalid_placement_fields",
            )
            placements.append(Placement(**placement_data))
        stages.append(
            Stage(
                stage_id=stage_data["stage_id"],
                layer_range=LayerRange(**layer),
                component_roles=tuple(stage_data["component_roles"]),
                stage_cost=StageCost(**cost),
                placements=tuple(placements),
            )
        )

    def parse_edges(value: Any, code: str) -> tuple[PlacementEdge, ...]:
        _require(isinstance(value, list), code)
        parsed: list[PlacementEdge] = []
        for edge_document in value:
            edge = _exact_fields(
                edge_document,
                {"edge_id", "from_placement_id", "to_placement_id", "link_id"},
                "invalid_edge_fields",
            )
            parsed.append(PlacementEdge(**edge))
        return tuple(parsed)

    graph = ExecutionGraph(
        deployment_id=root["deployment_id"],
        deployment_epoch=root["deployment_epoch"],
        topology_version=root["topology_version"],
        model_id=root["model_id"],
        resolved_commit=root["resolved_commit"],
        manifest_digest=root["manifest_digest"],
        entry_stage_id=root["entry_stage_id"],
        final_stage_id=root["final_stage_id"],
        hidden_size=root["hidden_size"],
        activation_bytes=root["activation_bytes"],
        token_envelope_bytes=root["token_envelope_bytes"],
        stages=tuple(stages),
        edges=parse_edges(root["edges"], "invalid_edges"),
        loopback_edges=parse_edges(root["loopback_edges"], "invalid_loopback_edges"),
        protocol=root["protocol"],
    )
    try:
        return validate_execution_graph(graph)
    except (TypeError, ValueError, AttributeError) as exc:
        raise NodeCommandError("invalid_execution_graph") from exc


def device_states_from_document(
    document: Any,
    *,
    observed_at: float | None = None,
) -> dict[str, DeviceState]:
    _require(isinstance(document, dict) and bool(document), "invalid_device_states")
    expected = {
        "node_id",
        "state_seq",
        "last_updated",
        "availability",
        "compute_units_per_second",
        "free_compute_fraction",
        "available_kv_bytes",
        "pending_hop_queue_depth",
        "neighbor_rtt_ms",
        "neighbor_bandwidth_bytes_per_second",
    }
    states: dict[str, DeviceState] = {}
    now = time.time() if observed_at is None else observed_at
    for node_id, state_document in document.items():
        state_data = _exact_fields(
            state_document, expected, "invalid_device_state_fields"
        )
        _require(state_data["node_id"] == node_id, "device_state_key_mismatch")
        _require(
            isinstance(state_data["neighbor_rtt_ms"], dict), "invalid_neighbor_rtt"
        )
        _require(
            isinstance(state_data["neighbor_bandwidth_bytes_per_second"], dict),
            "invalid_neighbor_bandwidth",
        )
        state = DeviceState(**state_data)
        states[node_id] = replace(state, last_updated=now)
    return states


class _UuidSource:
    def new(self, namespace: str) -> str:
        _require(isinstance(namespace, str) and bool(namespace), "invalid_id_namespace")
        return f"{namespace}-{uuid.uuid4()}"


class _DistributedProtocolClock:
    """Unix-aligned time that advances monotonically within this process.

    Lease deadlines cross physical hosts, so raw ``time.monotonic()`` values
    cannot enter Router/runtime protocol state: each host has a different
    monotonic epoch.  Anchor once to the shared Unix domain, then advance from
    the local monotonic source to avoid wall-clock jumps during a run.
    """

    def __init__(
        self,
        *,
        unix_now: Callable[[], float] | None = None,
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        self._monotonic_now = monotonic_now or time.monotonic
        wall_source = unix_now or time.time
        self._unix_origin = float(wall_source())
        self._monotonic_origin = float(self._monotonic_now())

    def now(self) -> float:
        return self._unix_origin + (
            float(self._monotonic_now()) - self._monotonic_origin
        )


class _CaptureSink:
    def __init__(self) -> None:
        self.token_ids: list[int] = []
        self.token_indexes: list[int] = []

    def emit(self, token_index: int, token_id: int) -> None:
        self.token_indexes.append(token_index)
        self.token_ids.append(token_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "token_indexes": list(self.token_indexes),
            "token_ids": list(self.token_ids),
        }


#: Distinct sidecar event names tracked per process; more fold into "other".
_SIDECAR_EVENT_NAMES_LIMIT = 64
#: One sidecar output line is read in at most this many characters.
_SIDECAR_LINE_LIMIT = 65_536
_SIDECAR_EVENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,60}\Z")


def _drain_sidecar_stream(
    stream: Any,
    counts: dict[str, int],
    lock: threading.Lock,
) -> None:
    """Read one sidecar output stream to EOF; forward bounded event lines.

    The sidecar writes ``address_changed`` records to stdout and one JSON
    line per transport event to stderr for its whole life.  Left unread, a
    full pipe blocks the sidecar's next write - inside its outbound worker for
    connection events - and every outbound frame stops.  Each JSON ``event``
    is forwarded to this node's stderr as ``MYCELIUM-SIDECAR-EVENT
    event=<name> [connection_generation=<n>] [count=<n>]`` on its 1st, 2nd,
    4th, 8th ... occurrence: bounded under a flood, and only the event name
    and generation - never endpoint ids, addresses or free text (a line that
    is not such a record counts as ``unparsed``).
    """

    try:
        while True:
            raw = stream.readline(_SIDECAR_LINE_LIMIT)
            if not raw:
                return
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            name = "unparsed"
            generation: int | None = None
            try:
                document = json.loads(line)
            except ValueError:
                document = None
            if isinstance(document, dict):
                event = document.get("event")
                if isinstance(event, str) and _SIDECAR_EVENT_NAME.fullmatch(event):
                    name = event
                    value = document.get("connection_generation")
                    if type(value) is int and 0 <= value <= (1 << 64) - 1:
                        generation = value
            with lock:
                if name not in counts and len(counts) >= _SIDECAR_EVENT_NAMES_LIMIT:
                    name = "other"
                count = counts.get(name, 0) + 1
                counts[name] = count
            if count & (count - 1):
                continue
            parts = [f"event={name}"]
            if generation is not None:
                parts.append(f"connection_generation={generation}")
            if count > 1:
                parts.append(f"count={count}")
            try:
                _emit_stderr_line("MYCELIUM-SIDECAR-EVENT " + " ".join(parts))
            except Exception:
                pass
    except Exception:
        return


class NativeSidecarProcess:
    """Launch native sidecar with bootstrap bytes inherited through a pipe only."""

    def __init__(
        self,
        *,
        binary: Path,
        socket_root: Path,
        local_only: bool,
        queue_capacity: int,
        startup_timeout: float,
        endpoint_secret_file: Path | None = None,
        force_relay: bool = False,
        loopback: bool | None = None,
        lan: bool = False,
    ) -> None:
        _require(
            isinstance(local_only, bool)
            and isinstance(force_relay, bool)
            and isinstance(lan, bool)
            and not (local_only and force_relay)
            and not (lan and (local_only or force_relay)),
            "invalid_sidecar_network_mode",
        )
        # Windows has no Unix-domain sidecar socket: the sidecar serves the
        # local Router on an authenticated 127.0.0.1 port instead
        # (native/iroh_transport/src/local_ipc.rs).
        self.loopback = mycelium_host_platform.is_win32() if loopback is None else loopback
        _require(isinstance(self.loopback, bool), "invalid_sidecar_network_mode")
        self.binary = binary
        self.socket_root = socket_root
        self.local_only = local_only
        self.force_relay = force_relay
        self.lan = lan
        self.queue_capacity = queue_capacity
        self.startup_timeout = startup_timeout
        self.endpoint_secret_file = _validated_endpoint_secret_file(
            endpoint_secret_file
        )
        self.socket_path: Path | LoopbackEndpoint = socket_root / "i.sock"
        self._bootstrap_material: bytes | None = None
        self._socket_root_created = False
        self.process: subprocess.Popen[str] | None = None
        self.ready: dict[str, Any] | None = None
        self._stream_drains: dict[int, threading.Thread] = {}
        self._event_counts: dict[str, int] = {}
        self._event_lock = threading.Lock()

    def _start_stream_drains(self, process: Any) -> None:
        """Keep both output pipes drained for the sidecar's lifetime."""

        for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            if stream is None:
                continue
            thread = threading.Thread(
                target=_drain_sidecar_stream,
                args=(stream, self._event_counts, self._event_lock),
                name=f"mycelium-sidecar-{label}",
                daemon=True,
            )
            self._stream_drains[id(stream)] = thread
            thread.start()

    def sidecar_event_counts(self) -> dict[str, int]:
        with self._event_lock:
            return dict(self._event_counts)

    @property
    def bootstrap_material(self) -> bytes:
        _require(self._bootstrap_material is not None, "sidecar_not_started")
        return self._bootstrap_material

    def status(self) -> dict[str, Any]:
        """Return bounded child health without exposing process identity."""

        process = self.process
        returncode = None if process is None else process.poll()
        return {
            "started": process is not None,
            "alive": process is not None and returncode is None,
            "returncode": returncode,
        }

    def _argv(self, bootstrap_fd: int) -> list[str]:
        command = [
            str(self.binary),
            "--uds",
            str(self.socket_path),
            "--bootstrap-fd",
            str(bootstrap_fd),
            "--queue-capacity",
            str(self.queue_capacity),
        ]
        if self.local_only:
            command.append("--local-only")
        if self.force_relay:
            command.append("--force-relay")
        if self.lan:
            command.append("--lan")
        if self.endpoint_secret_file is not None:
            command.extend(["--endpoint-secret-file", str(self.endpoint_secret_file)])
        return command

    def _loopback_argv(self) -> list[str]:
        command = [
            str(self.binary),
            "--loopback",
            "--bootstrap-stdin",
            "--allowed-client-pid",
            str(os.getpid()),
            "--queue-capacity",
            str(self.queue_capacity),
        ]
        if self.local_only:
            command.append("--local-only")
        if self.force_relay:
            command.append("--force-relay")
        if self.lan:
            command.append("--lan")
        if self.endpoint_secret_file is not None:
            command.extend(["--endpoint-secret-file", str(self.endpoint_secret_file)])
        return command

    def _start_loopback(self) -> dict[str, Any]:
        """Windows: bootstrap bytes on a stdin pipe, Router on a loopback port."""

        material = os.urandom(32)
        process = subprocess.Popen(
            self._loopback_argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.process = process
        try:
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(material)
                process.stdin.flush()
            finally:
                process.stdin.close()
            lines: list[bytes] = []
            reader = threading.Thread(
                target=lambda: lines.append(process.stdout.readline()),
                name="sidecar-ready",
                daemon=True,
            )
            reader.start()
            reader.join(self.startup_timeout)
            _require(not reader.is_alive(), "sidecar_start_timeout")
            _require(bool(lines and lines[0]), "sidecar_exited_before_ready")
            ready = json.loads(lines[0].decode("utf-8"))
            _require(
                isinstance(ready, dict)
                and ready.get("event") == "ready"
                and ready.get("alpn") == "mycelium.iroh.sidecar.v1"
                and isinstance(ready.get("endpoint_id"), str)
                and isinstance(ready.get("endpoint_addr"), dict)
                and ready["endpoint_addr"].get("id") == ready["endpoint_id"]
                and type(ready.get("local_port")) is int,
                "invalid_sidecar_ready_record",
            )
            self._start_stream_drains(process)
            try:
                endpoint = LoopbackEndpoint(ready["local_port"])
            except ValueError:
                _require(False, "invalid_sidecar_ready_record")
            client = SidecarClient(endpoint, material, timeout=self.startup_timeout)
            try:
                client.connect()
                _require(
                    client.endpoint_id == ready["endpoint_id"],
                    "sidecar_endpoint_mismatch",
                )
                client.ping()
            finally:
                client.close()
            self.socket_path = endpoint
            self._bootstrap_material = material
            self.ready = ready
            return ready
        except BaseException:
            self.close()
            raise

    def start(self) -> dict[str, Any]:
        _require(self.process is None, "sidecar_already_started")
        _require(
            self.binary.is_file() and os.access(self.binary, os.X_OK),
            "sidecar_binary_unavailable",
        )
        if self.loopback:
            return self._start_loopback()
        self._prepare_socket_root()
        _require(
            len(os.fsencode(self.socket_path)) < 100, "sidecar_socket_path_too_long"
        )
        material = os.urandom(32)
        read_fd, write_fd = os.pipe()
        try:
            try:
                os.write(write_fd, material)
            finally:
                os.close(write_fd)
            command = self._argv(read_fd)
            try:
                process = subprocess.Popen(
                    command,
                    pass_fds=(read_fd,),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            finally:
                os.close(read_fd)
        except BaseException:
            try:
                os.close(read_fd)
            except OSError:
                pass
            raise
        self.process = process
        try:
            assert process.stdout is not None
            readable, _, _ = select.select(
                [process.stdout], [], [], self.startup_timeout
            )
            _require(bool(readable), "sidecar_start_timeout")
            line = process.stdout.readline()
            _require(bool(line), "sidecar_exited_before_ready")
            ready = json.loads(line)
            _require(
                isinstance(ready, dict)
                and ready.get("event") == "ready"
                and ready.get("alpn") == "mycelium.iroh.sidecar.v1"
                and isinstance(ready.get("endpoint_id"), str)
                and isinstance(ready.get("endpoint_addr"), dict)
                and ready["endpoint_addr"].get("id") == ready["endpoint_id"],
                "invalid_sidecar_ready_record",
            )
            self._start_stream_drains(process)
            client = SidecarClient(
                self.socket_path, material, timeout=self.startup_timeout
            )
            try:
                client.connect()
                _require(
                    client.endpoint_id == ready["endpoint_id"],
                    "sidecar_endpoint_mismatch",
                )
                client.ping()
            finally:
                client.close()
            self._bootstrap_material = material
            self.ready = ready
            return ready
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process = self.process
        if process is not None:
            try:
                if process.poll() is None:
                    if self.loopback:
                        # No console Ctrl+C to a detached child on Windows.
                        process.terminate()
                    else:
                        process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                for stream in (process.stdout, process.stderr):
                    if stream is None:
                        continue
                    drain = self._stream_drains.pop(id(stream), None)
                    if drain is not None:
                        # The child has exited, so the pipe reaches EOF and the
                        # drain returns.  Closing under a blocked reader would
                        # wait on the reader's buffer lock: if a descendant
                        # still holds the pipe, leave the closed-at-exit to GC.
                        drain.join(1.0)
                        if drain.is_alive():
                            continue
                    if not stream.closed:
                        try:
                            stream.close()
                        except OSError:
                            pass
            finally:
                self.process = None
                self._bootstrap_material = None
                self.ready = None
                self._cleanup_socket_root()
        else:
            self._bootstrap_material = None
            self.ready = None
            self._cleanup_socket_root()

    def _cleanup_socket_root(self) -> None:
        if not self._socket_root_created:
            return
        try:
            socket_metadata = self.socket_path.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        else:
            if not stat.S_ISDIR(socket_metadata.st_mode):
                try:
                    self.socket_path.unlink()
                except OSError:
                    pass
        try:
            root_metadata = self.socket_root.lstat()
        except FileNotFoundError:
            self._socket_root_created = False
            return
        except OSError:
            return
        if stat.S_ISDIR(root_metadata.st_mode):
            try:
                self.socket_root.rmdir()
            except OSError:
                return
            self._socket_root_created = False

    #: How long a fresh start waits for a previous owner of the same socket
    #: root to finish exiting. A cold route restart relaunches the node while
    #: the retired route's sidecar (or a worker behind a dropped control link)
    #: is still draining; this is the cancel-and-cleanup budget an owner is
    #: allowed to take. A live owner that outlasts it is a real conflict.
    socket_conflict_grace_seconds: float = mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS
    _SOCKET_CONFLICT_POLL_SECONDS = 0.25

    def _prepare_socket_root(self) -> None:
        """Create the sidecar socket root, clearing crashed-cycle residue.

        A socket root left behind by a crashed cycle (orphaned sidecar)
        must not wedge a fresh configure. A LIVE sidecar — an accepting
        socket — still fails closed: the create-exclusive mkdir that
        follows the cleanup preserves the original mutual exclusion
        between two node instances. A live owner gets a bounded grace to
        finish exiting (its socket stops accepting and is removed) before
        the conflict is raised; a live owner that outlasts it is never
        touched.
        """
        deadline = time.monotonic() + max(0.0, self.socket_conflict_grace_seconds)
        while True:
            try:
                self._prepare_socket_root_once()
                return
            except NodeCommandError as exc:
                if (
                    exc.code != "sidecar_socket_conflict"
                    or time.monotonic() >= deadline
                ):
                    raise
            time.sleep(self._SOCKET_CONFLICT_POLL_SECONDS)

    def _prepare_socket_root_once(self) -> None:
        try:
            self.socket_root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if self.socket_path.exists():
                _require(not self._socket_is_live(), "sidecar_socket_conflict")
                try:
                    self.socket_path.unlink()
                except OSError:
                    _require(False, "sidecar_socket_conflict")
            try:
                self.socket_root.rmdir()
            except OSError:
                _require(False, "sidecar_socket_conflict")
            self.socket_root.mkdir(parents=True, exist_ok=False)
        self._socket_root_created = True

    def _socket_is_live(self) -> bool:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.25)
        try:
            client.connect(str(self.socket_path))
        except OSError:
            return False
        else:
            client.close()
            return True


class PhysicalNodeService:
    def __init__(
        self,
        *,
        run_id: str,
        deployment_id: str,
        node_id: str,
        artifact_root: Path,
        socket_root: Path,
        sidecar_binary: Path,
        sidecar_local_only: bool,
        command_timeout: float,
        endpoint_secret_file: Path | None = None,
        requested_decode_mode: str | None = None,
        sidecar_force_relay: bool = False,
        capacity_root: Path | None = None,
        sidecar_lan: bool = False,
    ) -> None:
        for value, code in (
            (run_id, "invalid_run_id"),
            (deployment_id, "invalid_deployment_id"),
            (node_id, "invalid_node_id"),
        ):
            _require(
                isinstance(value, str) and bool(value) and value == value.strip(), code
            )
        _require(command_timeout > 0, "invalid_command_timeout")
        _require(
            isinstance(sidecar_local_only, bool)
            and isinstance(sidecar_force_relay, bool)
            and isinstance(sidecar_lan, bool)
            and not (sidecar_local_only and sidecar_force_relay)
            and not (sidecar_lan and (sidecar_local_only or sidecar_force_relay)),
            "invalid_sidecar_network_mode",
        )
        _require(
            requested_decode_mode in {None, "complete_context_replay", "stage_local_kv"},
            "invalid_requested_decode_mode",
        )
        self.run_id = run_id
        self.deployment_id = deployment_id
        self.node_id = node_id
        self.artifact_root = artifact_root.resolve(strict=True)
        self.socket_root = socket_root
        if capacity_root is not None and mycelium_host_platform.is_win32():
            _require(_win32_private_root(capacity_root), "invalid_capacity_root")
        elif capacity_root is not None:
            _require(capacity_root.is_absolute()
                     and capacity_root.resolve(strict=True) == capacity_root
                     and capacity_root.is_dir()
                     and capacity_root.stat().st_uid == os.getuid()
                     and stat.S_IMODE(capacity_root.stat().st_mode) == 0o700,
                     "invalid_capacity_root")
        self.capacity_root = socket_root.parent if capacity_root is None else capacity_root
        self.sidecar_binary = sidecar_binary.resolve(strict=False)
        self.sidecar_local_only = sidecar_local_only
        self.sidecar_force_relay = sidecar_force_relay
        self.sidecar_lan = sidecar_lan
        self.command_timeout = command_timeout
        self.endpoint_secret_file = _validated_endpoint_secret_file(
            endpoint_secret_file
        )
        self.requested_decode_mode = requested_decode_mode
        self.host_id, self.boot_id = derive_local_run_scoped_identity(run_id)
        self.process_id = os.getpid()
        self.state = "NEW"
        self.stop_requested = False
        self.graph: ExecutionGraph | None = None
        self.topology: PublishedTopologyProvider | None = None
        self.device_states: PublishedDeviceStateProvider | None = None
        self.capacity: SQLiteQualificationCapacityPort | None = None
        self.runtime: Any = None
        self.runtime_identity: dict[str, str] | None = None
        # A7: staged KV-successor roles (``a7/node.json``); ``None`` = A6 only.
        self._a7_wiring: Any = None
        # A11: staged draft binding (``a11/node.json``); ``None`` = target-only.
        self._a11_wiring: Any = None
        self.sidecar: NativeSidecarProcess | None = None
        self.transport: IrohTransport | None = None
        self.router: Router | None = None
        self.signer: Any | None = None
        self.endpoint_id: str | None = None
        self.endpoint_addr: dict[str, Any] | None = None
        self.peer_generation = 0
        self._ids = _UuidSource()
        self._clock = _DistributedProtocolClock()
        self._router_config = RouterConfig(
            prefill_chunk_size_tokens=(
                8 if requested_decode_mode == "stage_local_kv" else 0
            )
        )
        self._sinks: dict[str, _CaptureSink] = {}
        self._last_cancellation: dict[str, Any] | None = None
        self._cancellations_by_subject: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._control_lock = threading.RLock()
        self._request_controls: dict[str, dict[str, Any]] = {}
        # Work a command defers until its response has been emitted (A7
        # accept-first: the route binds the claim before the cutover starts).
        self._after_response = threading.local()
        self._pending_cancellations: dict[str, dict[str, Any]] = {}
        self._cancellation_controls: dict[str, dict[str, Any]] = {}
        self._cancellation_workers: dict[str, threading.Thread] = {}
        # ``infer_cancel_wait`` may wait briefly for the exact receipt produced
        # by its independently running teardown worker.  The event is only a
        # delivery optimization: the signed request-scoped receipt remains the
        # sole cleanup authority.
        self._cancellation_receipt_events: dict[str, threading.Event] = {}
        # A worker disappearing is not, by itself, cleanup proof.  Retain a
        # bounded request-scoped failure marker until the same generation-
        # fenced teardown succeeds.  Without this marker an exception in the
        # daemon worker's body was erased by its ``finally`` block and a later
        # snapshot could mistake "thread absent" for "teardown complete".
        self._cancellation_worker_errors: dict[str, str] = {}
        self._request_cleanup_receipts: dict[str, dict[str, Any]] = {}
        self._request_cleanup_receipt_counters: dict[str, dict[str, int]] = {}

    def _safe_document(self, relative_path: Any, code: str) -> dict[str, Any]:
        _require(
            isinstance(relative_path, str)
            and bool(relative_path)
            and len(relative_path) <= 1024,
            code,
        )
        relative = PurePosixPath(relative_path)
        _require(
            not relative.is_absolute()
            and str(relative) == relative_path
            and 0 < len(relative.parts) <= 16
            and all(part not in {"", ".", ".."} for part in relative.parts),
            code,
        )
        if mycelium_host_platform.is_win32():
            return self._win32_safe_document(relative, relative_path, code)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        cloexec = getattr(os, "O_CLOEXEC", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        root_fd: int | None = None
        current_fd: int | None = None
        document_fd: int | None = None

        def fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
            return (
                metadata.st_mode,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                metadata.st_nlink,
            )

        try:
            root_before = os.stat(self.artifact_root, follow_symlinks=False)
            root_fd = os.open(
                self.artifact_root,
                os.O_RDONLY | directory | nofollow | cloexec,
            )
            current_fd = root_fd
            opened_root = os.fstat(root_fd)
            _require(
                stat.S_ISDIR(opened_root.st_mode)
                and fingerprint(root_before) == fingerprint(opened_root),
                code,
            )
            for part in relative.parts[:-1]:
                child_fd = os.open(
                    part,
                    os.O_RDONLY | directory | nofollow | cloexec,
                    dir_fd=current_fd,
                )
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = child_fd
            document_fd = os.open(
                relative.parts[-1],
                os.O_RDONLY | nofollow | cloexec,
                dir_fd=current_fd,
            )
            before = os.fstat(document_fd)
            _require(
                stat.S_ISREG(before.st_mode)
                and before.st_nlink == 1
                and 0 < before.st_size <= MAX_COMMAND_BYTES,
                code,
            )
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(document_fd, min(remaining, 1024 * 1024))
                _require(bool(chunk), code)
                chunks.append(chunk)
                remaining -= len(chunk)
            _require(fingerprint(os.fstat(document_fd)) == fingerprint(before), code)
            root_after = os.stat(self.artifact_root, follow_symlinks=False)
            _require(fingerprint(root_after) == fingerprint(opened_root), code)
            raw = b"".join(chunks)
        except NodeCommandError:
            raise
        except (OSError, ValueError) as exc:
            raise NodeCommandError(code) from exc
        finally:
            if document_fd is not None:
                os.close(document_fd)
            if current_fd is not None and current_fd != root_fd:
                os.close(current_fd)
            if root_fd is not None:
                os.close(root_fd)
        try:
            document = canonical_json_loads(raw, path=relative_path)
        except Exception as exc:
            raise NodeCommandError(code) from exc
        _require(isinstance(document, dict), code)
        return document

    def _win32_safe_document(
        self, relative: PurePosixPath, relative_path: str, code: str
    ) -> dict[str, Any]:
        """Read one control document below the artifact root through handles."""

        try:
            with mycelium_host_platform.open_beneath(
                self.artifact_root, tuple(relative.parts)
            ) as handle:
                before = os.fstat(handle.fileno())
                _require(
                    stat.S_ISREG(before.st_mode)
                    and before.st_nlink == 1
                    and 0 < before.st_size <= MAX_COMMAND_BYTES,
                    code,
                )
                raw = handle.read(MAX_COMMAND_BYTES + 1)
                after = os.fstat(handle.fileno())
                _require(
                    len(raw) == before.st_size
                    and (after.st_size, after.st_mtime_ns)
                    == (before.st_size, before.st_mtime_ns),
                    code,
                )
        except NodeCommandError:
            raise
        except (OSError, ValueError, mycelium_host_platform.HostPlatformError) as exc:
            raise NodeCommandError(code) from exc
        try:
            document = canonical_json_loads(raw, path=relative_path)
        except Exception as exc:
            raise NodeCommandError(code) from exc
        _require(isinstance(document, dict), code)
        return document

    def _identity(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "deployment_id": self.deployment_id,
            "node_id": self.node_id,
            "host_id": self.host_id,
            "process_id": self.process_id,
            "endpoint_id": self.endpoint_id,
            "peer_generation": self.peer_generation,
            "state": self.state,
            "route_ready": False,
        }

    def _signed_result(
        self, event: str, details: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        _require(self.signer is not None, "signer_not_ready")
        statement = {
            "protocol": NODE_OBSERVATION_PROTOCOL,
            "event": event,
            "monotonic_ns": time.monotonic_ns(),
            **self._identity(),
            "details": {} if details is None else _plain_json(details),
        }
        return {
            "observation": statement,
            "signature": self.signer.sign(statement),
            "verification_key": self.signer.public_key_record(),
        }

    def _host_resources(self) -> dict[str, Any]:
        _require(self.runtime_identity is not None, "runtime_identity_unavailable")
        runtime_identity = self.runtime_identity
        backend = runtime_identity["backend"]
        from model_adapters import ADAPTERS

        supported_architectures = sorted(
            adapter.architecture
            for adapter in ADAPTERS.values()
            if backend in adapter.runtime_backends
        )
        supported_dtypes = {"float32", "float16", "bfloat16"}
        supported_quantizations = {
            "none",
            "float32",
            "float16",
            "bfloat16",
            "int8-weight-only",
        }
        supported_dtypes.add(runtime_identity["dtype"])
        supported_quantizations.add(runtime_identity["quantization"])
        decode_modes_by_architecture = {
            architecture: (
                ["complete_context_replay", "stage_local_kv"]
                if backend == "mlx"
                or (backend == "numpy" and architecture in {"qwen2", "qwen3"})
                else ["complete_context_replay"]
            )
            for architecture in supported_architectures
        }
        supported_decode_modes = sorted(
            {mode for modes in decode_modes_by_architecture.values() for mode in modes}
        )
        object_root = self.artifact_root / ".mycelium" / "objects" / "sha256"
        cached_content_digests = (
            sorted(
                f"sha256:{path.name}"
                for path in object_root.iterdir()
                if path.is_file() and re.fullmatch(r"[0-9a-f]{64}", path.name)
            )
            if object_root.is_dir()
            else []
        )
        _require(
            len(cached_content_digests) <= 4096,
            "cached_content_digest_limit_exceeded",
        )
        disk = shutil.disk_usage(self.artifact_root)
        now_unix_ms = int(time.time() * 1_000)
        document: dict[str, Any] = {
            "protocol": "mycelium.host_resource_snapshot.v1",
            "observed_at_unix_ms": now_unix_ms,
            "valid_until_unix_ms": now_unix_ms + 120_000,
            "backend": backend,
            "supported_architectures": supported_architectures,
            "supported_dtypes": sorted(supported_dtypes),
            "supported_quantizations": sorted(supported_quantizations),
            "supported_decode_modes": supported_decode_modes,
            "decode_modes_by_architecture": decode_modes_by_architecture,
            "runtime_build_digest": _runtime_build_digest(backend),
            "available_memory_bytes": _host_available_memory_bytes(),
            "rss_bytes": _process_rss_bytes(),
            "swap_used_bytes": _host_swap_used_bytes(),
            "disk_free_bytes": disk.free,
            "disk_total_bytes": disk.total,
            "cached_content_digests": cached_content_digests,
            "thermal_state": _host_thermal_state(),
            "power_state": _host_power_state(),
            "route_ready": False,
        }
        document["resource_digest"] = (
            "sha256:" + hashlib.sha256(canonical_json_bytes(document)).hexdigest()
        )
        return document

    def _new_sidecar_process(self) -> NativeSidecarProcess:
        return NativeSidecarProcess(
            binary=self.sidecar_binary,
            socket_root=self.socket_root,
            local_only=self.sidecar_local_only,
            force_relay=self.sidecar_force_relay,
            queue_capacity=128,
            startup_timeout=min(self.command_timeout, 30.0),
            endpoint_secret_file=self.endpoint_secret_file,
            lan=self.sidecar_lan,
        )

    @staticmethod
    def _validate_pixel_assignment_binding(
        assignment: dict[str, Any],
        pixel_stage: Any,
        graph: ExecutionGraph,
        graph_stage: Stage,
        placement: Placement,
    ) -> None:
        document = pixel_stage.document
        string_fields = (
            "deployment_id",
            "model_id",
            "resolved_commit",
            "manifest_digest",
            "node_id",
            "assignment_id",
        )
        _require(
            all(type(assignment.get(field)) is str for field in string_fields)
            and type(assignment.get("deployment_epoch")) is int,
            "pixel_assignment_pack_mismatch",
        )
        expected_scalars = {
            "deployment_id": graph.deployment_id,
            "deployment_epoch": graph.deployment_epoch,
            "model_id": graph.model_id,
            "resolved_commit": graph.resolved_commit,
            "manifest_digest": graph.manifest_digest,
            "node_id": placement.node_id,
            "assignment_id": placement.assignment_id,
        }
        _require(
            all(
                assignment.get(field) == expected
                for field, expected in expected_scalars.items()
            ),
            "pixel_assignment_pack_mismatch",
        )
        _require(
            all(
                assignment.get(field) == document[field]
                for field in (
                    "deployment_id",
                    "assignment_id",
                    "model_id",
                    "resolved_commit",
                    "manifest_digest",
                )
            ),
            "pixel_assignment_pack_mismatch",
        )
        layer_range = assignment.get("range")
        expected_range = {
            "start_layer": graph_stage.layer_range.start_layer,
            "end_layer_exclusive": graph_stage.layer_range.end_layer_exclusive,
            "layer_count": graph_stage.layer_range.layer_count,
        }
        _require(
            isinstance(layer_range, dict)
            and set(layer_range) == set(expected_range)
            and all(type(layer_range.get(field)) is int for field in expected_range)
            and layer_range == expected_range
            and document["start_layer"] == expected_range["start_layer"]
            and document["end_layer_exclusive"]
            == expected_range["end_layer_exclusive"],
            "pixel_assignment_pack_mismatch",
        )
        roles = list(graph_stage.component_roles)
        _require(
            type(assignment.get("components")) is list
            and all(type(role) is str for role in assignment["components"])
            and assignment["components"] == roles
            and list(document["component_roles"]) == roles,
            "pixel_assignment_pack_mismatch",
        )
        tensor_keys = sorted(pixel_stage.tensors)
        component_tensor_keys = assignment.get("component_tensor_keys")
        expected_tensor_keys = assignment.get("expected_tensor_keys")
        expected_tensor_prefixes = assignment.get("expected_tensor_prefixes")
        _require(
            type(component_tensor_keys) is dict
            and set(component_tensor_keys) == {"decoder"}
            and type(component_tensor_keys["decoder"]) is list
            and all(type(key) is str for key in component_tensor_keys["decoder"])
            and component_tensor_keys["decoder"] == tensor_keys
            and type(expected_tensor_keys) is list
            and all(type(key) is str for key in expected_tensor_keys)
            and expected_tensor_keys == tensor_keys
            and type(expected_tensor_prefixes) is list
            and all(type(prefix) is str for prefix in expected_tensor_prefixes)
            and expected_tensor_prefixes == [pixel_stage.prefix],
            "pixel_assignment_pack_mismatch",
        )

    def _build_runtime_port(
        self,
        placement: Placement,
        graph: ExecutionGraph,
        loaded: Any,
        *,
        parent_assignment_digest: str | None = None,
    ) -> Any:
        """Lazily import and instantiate the placement's declared runtime port.

        The placement record selects the backend at runtime so that, for
        example, Linux nodes never import the Apple-only MLX backend and macOS
        Apple-Silicon nodes still receive the optimized path.  Both backends
        expose the RuntimePort protocol used by the Router.
        """
        backend = placement.runtime_backend
        clock = self._clock.now
        loaded_stages = {placement.placement_id: loaded}
        runtime_proof = getattr(loaded, "proof", None)
        runtime_document = (
            runtime_proof.get("runtime") if isinstance(runtime_proof, Mapping) else None
        )
        architecture = (
            runtime_document.get("architecture")
            if isinstance(runtime_document, Mapping)
            else None
        )
        supported_route_mode = _route_decode_mode(graph, architecture)
        if (
            self.requested_decode_mode == "stage_local_kv"
            and supported_route_mode != "stage_local_kv"
        ):
            raise NodeCommandError("requested_decode_mode_unsupported")
        route_decode_mode = self.requested_decode_mode or supported_route_mode
        if backend == "pixel-stdlib":
            from mycelium_mobile.pixel_runtime import (
                PixelRuntimeError,
                PixelStageRuntimePort,
            )
            from mycelium_mobile.pixel_stage import PixelStage

            _require(isinstance(loaded, PixelStage), "invalid_pixel_stage_pack_file")
            _require(
                isinstance(parent_assignment_digest, str)
                and bool(parent_assignment_digest),
                "invalid_parent_assignment_digest",
            )
            assert isinstance(parent_assignment_digest, str)
            try:
                return PixelStageRuntimePort(
                    loaded,
                    graph=graph,
                    placement_id=placement.placement_id,
                    parent_assignment_digest=parent_assignment_digest,
                )
            except PixelRuntimeError as exc:
                raise NodeCommandError("invalid_pixel_runtime_binding") from exc
        if backend == "numpy":
            from mycelium_router.numpy_runtime import NumpyRuntimePort

            return NumpyRuntimePort(
                self.node_id,
                graph,
                loaded_stages,
                clock=clock,
                decode_mode=route_decode_mode,
                kv_lease_renewal_seconds=self._kv_lease_window_seconds,
            )
        if backend == "mlx":
            from mycelium_router.mlx_runtime import MLXRuntimePort

            return MLXRuntimePort(
                self.node_id,
                graph,
                loaded_stages,
                clock=clock,
                decode_mode=route_decode_mode,
                kv_lease_renewal_seconds=self._kv_lease_window_seconds,
            )
        raise NodeCommandError("unsupported_runtime_backend")

    def _kv_lease_window_seconds(self) -> float:
        """KV lease TTL / progress-renewal window, scaled with the measured link.

        A14 serve-2 (f9a8d961 r1): a fixed 30 s lease issued at path build
        expired while a forced-relay prefill was still delivering a chunk
        every 3-6 s.  The window is the transport policy's
        ``min(ceiling, max(reservation_lease_seconds, W))``, where ``W`` is the
        same no-progress watchdog the prefill wait uses (all peers'
        conservative link estimate, chunk bytes ``8 x hidden x activation +
        4 KiB`` as before the first measured frame).  It issues each new
        reservation lease (path build) and renews a stage's KV on every
        applied continuation, so a slow but progressing link keeps its KV
        while work without progress for ``W`` still expires.
        """

        config = getattr(self, "_router_config", None)
        base = float(
            getattr(config, "reservation_lease_seconds", None)
            or RouterConfig.reservation_lease_seconds
        )
        transport = getattr(self, "transport", None)
        policy = getattr(transport, "stall_policy", None) or DEFAULT_PREFILL_STALL_POLICY
        link = None
        reader = getattr(transport, "link_estimate", None)
        if callable(reader):
            try:
                link = reader(None)
                if not (
                    float(link.goodput_bytes_per_second) > 0
                    and float(link.rtt_p95_seconds) > 0
                ):
                    link = None
            except Exception:
                link = None
        if link is None:
            link = SimpleNamespace(
                goodput_bytes_per_second=float(policy.unmeasured_goodput_bytes_per_second),
                rtt_p95_seconds=float(policy.unmeasured_rtt_seconds),
            )
        graph = getattr(self, "graph", None)
        hidden = getattr(graph, "hidden_size", 0)
        width = getattr(graph, "activation_bytes", 0)
        chunk_bytes = (
            8 * hidden * width + _PREFILL_FRAME_ENVELOPE_BYTES
            if type(hidden) is int and type(width) is int and hidden > 0 and width > 0
            else _PREFILL_FRAME_ENVELOPE_BYTES
        )
        try:
            return float(
                policy.kv_lease_seconds(
                    base_seconds=base, chunk_bytes=chunk_bytes, link=link
                )
            )
        except Exception:
            return base

    def _bind_router_config_to_runtime(self, runtime: Any) -> None:
        """Make Router prefill framing follow the runtime's resolved mode.

        ``requested_decode_mode`` may be omitted in a physical operator plan.
        In that case the runtime resolves the architecture-supported mode while
        the service is configured.  Basing chunking only on the optional CLI
        request leaves an auto-selected stage-local-KV runtime receiving one
        unbounded prefill operation, defeating its cooperative cancellation
        boundary.
        """

        mode = getattr(runtime, "decode_mode", None)
        _require(
            mode in {"complete_context_replay", "stage_local_kv"},
            "invalid_runtime_decode_mode",
        )
        self._router_config = replace(
            self._router_config,
            prefill_chunk_size_tokens=8 if mode == "stage_local_kv" else 0,
        )

    # -- A7 fenced KV-successor recovery -------------------------------------------

    def _a7_configure(self, graph: ExecutionGraph, runtime: Any) -> tuple[Any, Any]:
        """Bind the staged A7 roles; absent configuration leaves the node unchanged."""

        from mycelium_a7_node_wiring import (
            A7_NODE_CONFIG_PATH,
            A7WiringError,
            a7_node_config_path_for,
        )

        # One transfer bundle stages every node's own file under a7/<node_id>/;
        # a node-local a7/node.json is the single-node spelling.  Both is ambiguous.
        try:
            candidates: tuple[str, ...] = (a7_node_config_path_for(self.node_id), A7_NODE_CONFIG_PATH)
        except A7WiringError:
            candidates = (A7_NODE_CONFIG_PATH,)
        staged = [
            relative
            for relative in candidates
            if os.path.lexists(self.artifact_root / relative)
        ]
        if not staged:
            return runtime, None
        _require(len(staged) == 1, "a7_node_config_ambiguous")
        config_path = staged[0]
        from mycelium_a7_node_wiring import (
            A7NodeWiring,
            A7WiringError,
            validate_against_graph,
            validate_node_config,
        )
        from mycelium_router.standby_dispatch import StandbyDispatchRuntime

        try:
            config = validate_node_config(
                self._safe_document(config_path, "invalid_a7_node_config"),
                deployment_id=self.deployment_id,
                node_id=self.node_id,
            )
            validate_against_graph(config, graph, node_id=self.node_id)
            _require(
                getattr(runtime, "decode_mode", None) == "stage_local_kv",
                "a7_requires_stage_local_kv",
            )
            wiring = A7NodeWiring(
                config,
                node_id=self.node_id,
                read_document=lambda relative: self._safe_document(
                    relative, "invalid_a7_document"
                ),
                read_secret=self._a7_read_secret,
                fence_root=Path(self.capacity_root),
            )
            if config["standby"] is not None:
                placement = next(
                    item
                    for stage in graph.stages
                    for item in stage.placements
                    if item.placement_id == config["standby"]["placement_id"]
                )
                loaded = self._a7_load_standby_stage(config["standby"], placement)
                standby_port = self._build_runtime_port(placement, graph, loaded)
                wiring.bind_standby(standby_port)
                runtime = StandbyDispatchRuntime(
                    runtime, {placement.placement_id: standby_port}
                )
            wiring.load_qualifications(now_unix_ms=int(time.time() * 1000))
        except A7WiringError as exc:
            raise NodeCommandError(exc.code) from None
        except (ValueError, TypeError) as exc:
            raise NodeCommandError("invalid_a7_configuration") from exc
        return runtime, wiring

    def _a7_read_secret(self, relative_path: str) -> bytes:
        """Read the standby's staged owner-only secret (exact size, no symlinks)."""

        _require(
            isinstance(relative_path, str)
            and bool(relative_path)
            and not PurePosixPath(relative_path).is_absolute()
            and ".." not in PurePosixPath(relative_path).parts,
            "invalid_a7_secret_file",
        )
        from mycelium_a7_node_wiring import STANDBY_SECRET_BYTES

        path = self.artifact_root / relative_path
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise NodeCommandError("invalid_a7_secret_file") from exc
        try:
            metadata = os.fstat(descriptor)
            _require(
                stat.S_ISREG(metadata.st_mode)
                and (mycelium_host_platform.is_win32() or (
                    metadata.st_mode & 0o077 == 0
                    and metadata.st_uid == os.getuid()
                ))
                and metadata.st_size == STANDBY_SECRET_BYTES,
                "invalid_a7_secret_file",
            )
            secret = os.read(descriptor, STANDBY_SECRET_BYTES + 1)
        finally:
            os.close(descriptor)
        _require(len(secret) == STANDBY_SECRET_BYTES, "invalid_a7_secret_file")
        return secret

    def _a7_load_standby_stage(self, standby: Mapping[str, Any], placement: Placement) -> Any:
        """Verify and load the standby's own assignment-local stage pack."""

        from runtime_loader import canonical_json, load_assignment_stage
        from stage_pack import artifact_report_for_loader, verify_stage_pack

        assignment = self._safe_document(
            standby["assignment_file"], "invalid_a7_standby_assignment"
        )
        _require(
            assignment.get("deployment_id") == self.deployment_id
            and assignment.get("node_id") == self.node_id
            and assignment.get("assignment_id") == placement.assignment_id,
            "a7_standby_assignment_mismatch",
        )
        manifest = self._safe_document(standby["manifest_file"], "invalid_a7_manifest")
        pack = self._safe_document(standby["stage_pack_file"], "invalid_a7_stage_pack")
        try:
            verification = verify_stage_pack(pack, assignment=assignment, manifest=manifest)
            report = artifact_report_for_loader(
                pack, verification, assignment=assignment, manifest=manifest
            )
        except (TypeError, ValueError) as exc:
            raise NodeCommandError("invalid_a7_stage_pack") from exc
        loaded = load_assignment_stage(
            assignment, report, load_generation=standby["load_generation"]
        )
        _require(
            layer_load_proof_digest(json.loads(canonical_json(loaded.proof)))
            == placement.load_proof_digest,
            "a7_standby_load_proof_mismatch",
        )
        return loaded

    def _a11_configure(self, runtime: Any) -> Any:
        """Load the staged A11 draft binding; absent configuration stays target-only."""

        from mycelium_a11_node_wiring import (
            NODE_CONFIG_PATH as A11_NODE_CONFIG_PATH,
            A11WiringError,
            load_node_wiring,
            node_config_path_for,
            validate_node_config,
        )

        try:
            candidates: tuple[str, ...] = (node_config_path_for(self.node_id), A11_NODE_CONFIG_PATH)
        except A11WiringError:
            candidates = (A11_NODE_CONFIG_PATH,)
        staged = [
            relative
            for relative in candidates
            if os.path.lexists(self.artifact_root / relative)
        ]
        if not staged:
            return None
        _require(len(staged) == 1, "a11_node_config_ambiguous")
        _require(
            getattr(runtime, "decode_mode", None) == "stage_local_kv",
            "a11_requires_stage_local_kv",
        )
        try:
            config = validate_node_config(
                self._safe_document(staged[0], "invalid_a11_node_config"),
                deployment_id=self.deployment_id,
                node_id=self.node_id,
            )
            return load_node_wiring(self.artifact_root, config)
        except A11WiringError as exc:
            raise NodeCommandError(exc.code) from None

    def _a7_attach(self, router: Router, transport: Any) -> None:
        wiring = self._a7_wiring
        if wiring is None:
            return
        from mycelium_router.standby_dispatch import StandbyDispatchRuntime

        runtime = self.runtime
        primary = (
            runtime.primary if isinstance(runtime, StandbyDispatchRuntime) else runtime
        )
        assert self.graph is not None
        ports = {
            placement.placement_id: primary
            for stage in self.graph.stages
            for placement in stage.placements
            if placement.node_id == self.node_id
            and placement.lifecycle_state == "ACTIVE"
        }
        wiring.attach(
            router,
            send=transport.send_a7_message,
            primary_port=primary,
            ports_by_placement=ports,
        )
        if wiring.controller is not None:
            transport.forward_failure_observer = wiring.forward_failure_observer(
                router, node_id=self.node_id
            )
            wiring.controller.fence_old_path = transport.fence_path_forwards

    def _a7_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(
            isinstance(payload, dict)
            and set(payload) in ({"request_id"}, set())
            and (
                "request_id" not in payload
                or (isinstance(payload["request_id"], str) and bool(payload["request_id"]))
            ),
            "invalid_a7_snapshot_fields",
        )
        _require(self._a7_wiring is not None, "a7_not_configured")
        document = self._a7_wiring.snapshot(payload.get("request_id"))
        if "request_id" not in payload:
            window_ms = None
            relay = getattr(self.router, "relay", None) if self.router is not None else None
            config = getattr(getattr(relay, "batch_scheduler", None), "config", None)
            seconds = getattr(config, "live_hop_collection_window_seconds", None)
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0:
                window_ms = round(float(seconds) * 1_000.0, 3)
            product = self._a7_wiring.product_projection(
                now_unix_ms=int(time.time() * 1_000),
                command_timeout_ms=int(round(self.command_timeout * 1_000.0)),
                collection_window_ms=window_ms,
            )
            if product is not None:
                document["product"] = product
        return self._signed_result("a7_snapshot", document)

    def _a11_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Signed, read-only A11 gate evidence: KV release log and request history.

        Every node answers with its runtime's ordered KV release log (window
        residue); the entry node with a staged draft also answers with its
        speculation request history (proposal, acceptance, correction and
        fallback coverage).  Counts, closed reasons and request ids only.
        """

        # The collector's binding (window, phase, plan, prereg and, after the
        # benchmark, its document digest) is echoed into the signed details, so
        # a record cannot be replayed into another window or run.
        _require(isinstance(payload, dict) and set(payload) == {"binding"}, "invalid_a11_snapshot_fields")
        binding = _exact_fields(
            payload["binding"],
            {"window_id", "phase", "plan_digest", "prereg_digest", "benchmark_digest"},
            "invalid_a11_snapshot_binding",
        )
        _require(
            isinstance(binding["window_id"], str)
            and re.fullmatch(r"[A-Za-z0-9._-]{16,128}", binding["window_id"]) is not None
            and binding["phase"] in {"before", "after"}
            and all(
                isinstance(binding[key], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", binding[key]) is not None
                for key in ("plan_digest", "prereg_digest")
            )
            and (
                binding["benchmark_digest"] is None
                if binding["phase"] == "before"
                else isinstance(binding["benchmark_digest"], str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", binding["benchmark_digest"]) is not None
            ),
            "invalid_a11_snapshot_binding",
        )
        _require(self.state == "RUNNING" and self.runtime is not None, "invalid_state_for_a11_snapshot")
        release_log = getattr(self.runtime, "kv_release_log", None)
        _require(callable(release_log), "a11_kv_release_log_unavailable")
        wiring = getattr(self, "_a11_wiring", None)
        details = {
            "protocol": "mycelium.a11_node_evidence.v1",
            "binding": dict(binding),
            "kv": release_log(),
            "speculation": (
                None
                if wiring is None
                else {
                    "draft_identity_digest": wiring.model.identity_digest(),
                    "width": wiring.config["policy"]["width"],
                    "baseline_samples": wiring.baseline.count(),
                    "baseline_measured": wiring.baseline.measured() is not None,
                    **wiring.controller.evidence(),
                }
            ),
        }
        return self._signed_result("a11_snapshot", details)

    def _a11_draft_loss(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Grant-checked, request-scoped draft failure; target stage stays alive."""
        import hmac
        from mycelium_a11_route_speculation import RouteSpeculationError

        grant = "FLEET GRANTED TO A11"
        _require(
            os.environ.get("MYCELIUM_A11_FLEET_GRANT") == grant
            and isinstance(payload, dict)
            and isinstance(payload.get("grant"), str)
            and hmac.compare_digest(payload["grant"], grant),
            "explicit_fleet_grant_required",
        )
        _require(
            set(payload) == {"grant", "request_id", "after_accepted"}
            and isinstance(payload["request_id"], str)
            and 1 <= len(payload["request_id"]) <= 128
            and type(payload["after_accepted"]) is int
            and 1 <= payload["after_accepted"] <= 512,
            "invalid_a11_draft_loss_fields",
        )
        _require(self.state == "RUNNING", "a11_draft_loss_request_unavailable")
        wiring = getattr(self, "_a11_wiring", None)
        _require(wiring is not None, "a11_draft_loss_request_unavailable")
        try:
            wiring.controller.arm_draft_loss(payload["request_id"], payload["after_accepted"])
        except RouteSpeculationError as exc:
            raise NodeCommandError(exc.code) from exc
        return self._signed_result("a11_draft_loss", {
            "request_id": payload["request_id"],
            "after_accepted": payload["after_accepted"],
            "outcome": "armed",  # NOT a claim that loss has fired
        })

    def _defer_after_response(self, action: Callable[[], Any], *, name: str) -> None:
        pending = getattr(self._after_response, "actions", None)
        if pending is None:
            pending = []
            self._after_response.actions = pending
        pending.append((name, action))

    def run_after_response(self) -> None:
        """Start the work the just-answered command deferred (on its own thread)."""

        pending = getattr(self._after_response, "actions", None)
        self._after_response.actions = None
        for name, action in pending or ():
            threading.Thread(target=action, name=name, daemon=True).start()

    def discard_after_response(self) -> None:
        self._after_response.actions = None

    def _a7_source_lost(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A4 process-exit feed: hand one exited covered source to this entry.

        The route sends its exact request control and the exited node.  The
        entry accepts only for that bound control and a request whose covered
        source runs on that node, answers first, and only after the response is
        emitted reports the source PLACEMENT failure (fenced KV-successor
        cutover, or its truthful A6/abort fallback).
        """

        _require(
            isinstance(payload, dict)
            and set(payload) == {"request_id", "control", "lost_node_id"}
            and isinstance(payload["request_id"], str)
            and bool(payload["request_id"])
            and isinstance(payload["lost_node_id"], str)
            and bool(payload["lost_node_id"]),
            "invalid_a7_source_lost_fields",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_a7_source_lost",
        )
        _require(self._a7_wiring is not None, "a7_not_configured")
        control = self._validated_request_control(
            payload["control"],
            code="invalid_a7_source_lost_control",
            initial=False,
        )
        request_id = payload["request_id"]
        lost_node_id = payload["lost_node_id"]
        with self._control_lock:
            bound = self._request_controls.get(request_id)
        if lost_node_id == self.node_id:
            decision: dict[str, Any] = {"accepted": False, "reason": "lost_node_is_entry"}
        elif bound is None or dict(bound) != control:
            decision = {"accepted": False, "reason": "stale_request_control"}
        else:
            decision = self._a7_wiring.accept_source_lost(request_id, lost_node_id)
        result = self._signed_result(
            "a7_source_lost",
            {
                **decision,
                "request_id": request_id,
                "lost_node_id": lost_node_id,
                "control": dict(control),
            },
        )
        if decision.get("accepted") is True and decision.get("cutover_pending") is True:
            wiring = self._a7_wiring
            router = self.router
            self._defer_after_response(
                lambda: wiring.cut_over_lost_source(router, request_id, lost_node_id),
                name="a7-source-lost",
            )
        return result

    def _configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(self.state == "NEW", "invalid_state_for_configure")
        legacy_fields = {
            "assignment_file",
            "artifact_report_file",
            "graph",
            "device_states",
            "load_generation",
        }
        stage_pack_fields = {
            "assignment_file",
            "manifest_file",
            "stage_pack_file",
            "graph",
            "device_states",
            "load_generation",
        }
        pixel_stage_pack_fields = {
            "assignment_file",
            "pixel_stage_pack_file",
            "graph",
            "device_states",
            "load_generation",
        }
        _require(
            isinstance(payload, dict)
            and set(payload)
            in (legacy_fields, stage_pack_fields, pixel_stage_pack_fields),
            "invalid_configure_fields",
        )
        data = payload
        _require(
            isinstance(data["load_generation"], int)
            and not isinstance(data["load_generation"], bool)
            and data["load_generation"] > 0,
            "invalid_load_generation",
        )
        graph = execution_graph_from_document(data["graph"])
        _require(
            graph.deployment_id == self.deployment_id, "graph_deployment_id_mismatch"
        )
        states = device_states_from_document(
            data["device_states"],
            observed_at=self._clock.now(),
        )
        _require(
            set(states)
            == {
                placement.node_id
                for stage in graph.stages
                for placement in stage.placements
            },
            "device_state_node_mismatch",
        )
        assignment = self._safe_document(
            data["assignment_file"], "invalid_assignment_file"
        )
        _require(
            assignment.get("deployment_id") == self.deployment_id,
            "assignment_deployment_id_mismatch",
        )
        _require(
            assignment.get("node_id") == self.node_id, "assignment_node_id_mismatch"
        )
        stage_pack_digest: str | None = None
        report: dict[str, Any] | None = None
        pixel_stage: Any = None
        parent_assignment_digest: str | None = None
        loaded_runtime_identity: dict[str, str]
        if "pixel_stage_pack_file" in data:
            from layer_assignment import (
                LAYER_ASSIGNMENT_PROTOCOL,
                validate_assignment_identity,
            )
            from mycelium_mobile.pixel_stage import PixelStage, PixelStageError

            try:
                _require(
                    assignment.get("protocol") == LAYER_ASSIGNMENT_PROTOCOL,
                    "invalid_assignment_file",
                )
                validate_assignment_identity(assignment)
            except (KeyError, TypeError, ValueError) as exc:
                raise NodeCommandError("invalid_assignment_file") from exc
            runtime_identity = assignment.get("runtime")
            _require(
                isinstance(runtime_identity, dict)
                and set(runtime_identity) == {"backend", "dtype", "quantization"}
                and runtime_identity.get("backend") == "pixel-stdlib"
                and runtime_identity.get("dtype") == "float32"
                and runtime_identity.get("quantization") == "none",
                "invalid_assignment_file",
            )
            pixel_pack = self._safe_document(
                data["pixel_stage_pack_file"],
                "invalid_pixel_stage_pack_file",
            )
            try:
                pixel_stage = PixelStage.from_document(pixel_pack)
            except PixelStageError as exc:
                raise NodeCommandError("invalid_pixel_stage_pack_file") from exc
            _require(
                pixel_stage.document["run_id"] == self.run_id,
                "pixel_stage_run_id_mismatch",
            )
            parent_assignment_digest = (
                "sha256:" + hashlib.sha256(canonical_json_bytes(assignment)).hexdigest()
            )
            stage_pack_digest = pixel_pack["pack_digest"]
            loaded_runtime_identity = {
                "backend": "pixel-stdlib",
                "dtype": "float32",
                "quantization": "none",
                "architecture": "pixel_stage",
            }
        elif "stage_pack_file" in data:
            from stage_pack import artifact_report_for_loader, verify_stage_pack

            manifest = self._safe_document(
                data["manifest_file"],
                "invalid_manifest_file",
            )
            pack = self._safe_document(
                data["stage_pack_file"], "invalid_stage_pack_file"
            )
            try:
                verification = verify_stage_pack(
                    pack,
                    assignment=assignment,
                    manifest=manifest,
                )
                report = artifact_report_for_loader(
                    pack,
                    verification,
                    assignment=assignment,
                    manifest=manifest,
                )
            except (TypeError, ValueError) as exc:
                raise NodeCommandError("invalid_stage_pack_file") from exc
            stage_pack_digest = pack["stage_pack_digest"]
        else:
            report = self._safe_document(
                data["artifact_report_file"],
                "invalid_artifact_report_file",
            )
        local_bindings = [
            (stage, placement)
            for stage in graph.stages
            for placement in stage.placements
            if placement.node_id == self.node_id
            and placement.lifecycle_state == "ACTIVE"
        ]
        _require(len(local_bindings) == 1, "invalid_local_placement_count")
        graph_stage, placement = local_bindings[0]
        _require(
            placement.assignment_id == assignment.get("assignment_id"),
            "placement_assignment_mismatch",
        )
        if pixel_stage is not None:
            self._validate_pixel_assignment_binding(
                assignment,
                pixel_stage,
                graph,
                graph_stage,
                placement,
            )
            loaded = pixel_stage
        else:
            from runtime_loader import canonical_json, load_assignment_stage

            assert report is not None
            loaded = load_assignment_stage(
                assignment,
                report,
                load_generation=data["load_generation"],
            )
            load_proof_document = json.loads(canonical_json(loaded.proof))
            if layer_load_proof_digest(load_proof_document) != placement.load_proof_digest:
                _emit_load_proof_diagnostic(
                    load_proof_document,
                    expected_load_proof_digest=placement.load_proof_digest,
                )
                raise NodeCommandError("placement_load_proof_mismatch")
            proof_runtime_identity = load_proof_document.get("runtime_identity")
            _require(
                isinstance(proof_runtime_identity, dict)
                and all(
                    isinstance(proof_runtime_identity.get(field), str)
                    and bool(proof_runtime_identity[field])
                    for field in ("backend", "dtype", "quantization", "architecture")
                ),
                "invalid_runtime_identity",
            )
            loaded_runtime_identity = {
                field: proof_runtime_identity[field]
                for field in ("backend", "dtype", "quantization", "architecture")
            }
        topology = PublishedTopologyProvider(graph)
        device_provider = PublishedDeviceStateProvider(topology, states)
        capacity = SQLiteQualificationCapacityPort(
            self.capacity_root / f"{self.deployment_id}.capacity.sqlite3",
            topology,
            {node: state.available_kv_bytes for node, state in states.items()},
            clock=self._clock,
            id_source=self._ids,
            maximum_imported_lease_seconds=_maximum_imported_lease_seconds(
                self._router_config
            ),
            # A16: absent overlay file (owner not opted in) -> None -> unchanged behaviour.
            adaptive_gate=lambda: _read_adaptive_gate(self.capacity_root),
            adaptive_node_id=placement.node_id,
        )
        runtime = self._build_runtime_port(
            placement,
            graph,
            loaded,
            parent_assignment_digest=parent_assignment_digest,
        )
        self._bind_router_config_to_runtime(runtime)
        try:
            runtime, a7_wiring = self._a7_configure(graph, runtime)
            a11_wiring = self._a11_configure(runtime)
        except BaseException:
            runtime.close(reason="configure_failed")
            raise
        sidecar = self._new_sidecar_process()
        try:
            ready = sidecar.start()
        except BaseException:
            runtime.close(reason="configure_failed")
            raise
        self._a7_wiring = a7_wiring
        self._a11_wiring = a11_wiring
        self.graph = graph
        self.topology = topology
        self.device_states = device_provider
        self.capacity = capacity
        self.runtime = runtime
        self.runtime_identity = loaded_runtime_identity
        self.sidecar = sidecar
        endpoint_id = ready["endpoint_id"]
        _require(
            isinstance(endpoint_id, str) and bool(endpoint_id),
            "invalid_sidecar_ready",
        )
        self.endpoint_id = endpoint_id
        self.endpoint_addr = ready["endpoint_addr"]
        self.signer = (
            generate_ed25519_signer(endpoint_id=endpoint_id)
            if self.endpoint_secret_file is None
            else load_node_signer(
                self.endpoint_secret_file,
                endpoint_id=endpoint_id,
            )
        )
        self.state = "CONFIGURED"
        configured_details = {
            "assignment_id": assignment["assignment_id"],
            "placement_id": placement.placement_id,
            "manifest_digest": graph.manifest_digest,
            "endpoint_addr": self.endpoint_addr,
            "runtime_mode": runtime.decode_mode,
        }
        if stage_pack_digest is not None:
            if pixel_stage is not None:
                configured_details["pixel_stage_pack_digest"] = stage_pack_digest
            else:
                assert report is not None
                configured_details["stage_pack_digest"] = stage_pack_digest
                configured_details["stage_pack_verification_digest"] = report[
                    "stage_pack_verification_digest"
                ]
        if a7_wiring is not None:
            # A7: attest the exact validated staged roles (public records only,
            # never key material) so the route owner detects A7 staging from this
            # signed observation instead of reading a peer's disk.
            configured_details["a7_node_config"] = json.loads(json.dumps(a7_wiring.config))
        return self._signed_result("configured", configured_details)

    def _start(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(self.state == "CONFIGURED", "invalid_state_for_start")
        _require(
            isinstance(payload, dict)
            and set(payload)
            in (
                {"peer", "local_generation"},
                {"peer", "peers", "local_generation"},
            ),
            "invalid_start_fields",
        )
        data = payload
        local_generation = data["local_generation"]
        _require(
            isinstance(local_generation, int)
            and not isinstance(local_generation, bool)
            and 0 < local_generation <= (1 << 64) - 1,
            "invalid_local_generation",
        )
        peer_data = _exact_fields(
            data["peer"],
            {"node_id", "endpoint_id", "endpoint_addr", "generation"},
            "invalid_peer_fields",
        )
        try:
            peer = PeerBinding(**peer_data)
        except (TypeError, ValueError) as exc:
            raise NodeCommandError("invalid_peer_binding") from exc
        _require(peer.node_id != self.node_id, "self_peer_binding")
        additional_peer_documents = data.get("peers", [])
        _require(isinstance(additional_peer_documents, list), "invalid_peers")
        additional_peers: list[PeerBinding] = []
        for document in additional_peer_documents:
            peer_document = _exact_fields(
                document,
                {"node_id", "endpoint_id", "endpoint_addr", "generation"},
                "invalid_peer_fields",
            )
            try:
                additional_peer = PeerBinding(**peer_document)
            except (TypeError, ValueError) as exc:
                raise NodeCommandError("invalid_peer_binding") from exc
            _require(additional_peer.node_id != self.node_id, "self_peer_binding")
            additional_peers.append(additional_peer)
        all_peers = [peer, *additional_peers]
        _require(
            len({item.node_id for item in all_peers}) == len(all_peers),
            "duplicate_peer_node_id",
        )
        _require(
            len({item.endpoint_id for item in all_peers}) == len(all_peers),
            "duplicate_peer_endpoint_id",
        )
        assert self.sidecar is not None
        assert self.endpoint_id is not None
        assert self.runtime is not None
        assert self.topology is not None
        assert self.device_states is not None
        assert self.capacity is not None
        transport = IrohTransport(
            node_id=self.node_id,
            socket_path=self.sidecar.socket_path,
            bootstrap_secret=self.sidecar.bootstrap_material,
            peer=peer,
            peers=additional_peers,
            local_generation=local_generation,
            expected_endpoint_id=self.endpoint_id,
            queue_capacity=128,
            # A confirmed delivery acknowledges completed local Router dispatch.
            # Progressive prefill dispatch can synchronously execute and forward
            # through a slower downstream stage before the acknowledgement returns,
            # so a ten-second ceiling spuriously rejects healthy heterogeneous routes.
            delivery_timeout_seconds=min(
                self.command_timeout,
                _MAX_IROH_DELIVERY_TIMEOUT_SECONDS,
            ),
            poll_interval_seconds=0.02,
        )
        router = Router(
            node_id=self.node_id,
            topology=self.topology,
            device_states=self.device_states,
            capacity=self.capacity,
            runtime=self.runtime,
            transport=transport,
            clock=self._clock,
            id_source=self._ids,
            config=self._router_config,
            kv_lease_seconds=self._kv_lease_window_seconds,
        )
        try:
            transport.bind_router(router)
            # A14 class D: this node's own prefill stall recovery reaches its
            # stderr tail, which the route prints for every node on failure.
            transport.stall_event_observer = _print_transport_stall_event
            # A14 serve-2: every FailureReport's reason reaches this node's
            # stderr tail and its evidence bundle (both directions).
            transport.failure_report_observer = _print_failure_report_event
            self._a7_attach(router, transport)
            if self._a11_wiring is not None:
                self._a11_wiring.attach(router)
            transport.start()
        except BaseException:
            transport.close()
            raise
        self.transport = transport
        self.router = router
        self.peer_generation = peer.generation
        self.state = "RUNNING"
        return self._signed_result(
            "started",
            {
                "peer": _plain_json(peer),
                "peers": [_plain_json(item) for item in all_peers],
            },
        )

    def _validated_request_control(
        self,
        value: object,
        *,
        code: str,
        initial: bool,
    ) -> dict[str, Any]:
        control = _exact_fields(value, set(_REQUEST_CONTROL_FIELDS), code)
        _require(
            isinstance(control["deployment_id"], str)
            and bool(control["deployment_id"])
            and type(control["deployment_epoch"]) is int
            and control["deployment_epoch"] >= 1
            and isinstance(control["qualification_digest"], str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", control["qualification_digest"])
            is not None
            and isinstance(control["command_id"], str)
            and bool(control["command_id"])
            and len(control["command_id"].encode("utf-8")) <= 256
            and type(control["publisher_generation"]) is int
            and control["publisher_generation"] >= 1
            and type(control["absolute_deadline_ms"]) is int
            and control["absolute_deadline_ms"] > 0
            and type(control["request_attempt"]) is int
            and control["request_attempt"] >= 1
            and isinstance(control["path_id"], str)
            and bool(control["path_id"])
            and type(control["path_attempt"]) is int
            and control["path_attempt"] >= 0
            and isinstance(control["path_digest"], str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", control["path_digest"]) is not None
            and type(control["topology_generation"]) is int
            and control["topology_generation"] >= 1
            and type(control["cancellation_generation"]) is int
            and control["cancellation_generation"] >= 0
            and (not initial or control["cancellation_generation"] == 0)
            and self.graph is not None
            and control["deployment_id"] == self.graph.deployment_id
            and control["deployment_epoch"] == self.graph.deployment_epoch
            and control["topology_generation"] == self.graph.topology_version,
            code,
        )
        return control

    def _bind_request_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(
            payload,
            {"request_id", "control"},
            "invalid_bind_request_control_fields",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_bind_request_control",
        )
        _require(
            isinstance(data["request_id"], str) and bool(data["request_id"]),
            "invalid_request_id",
        )
        control = self._validated_request_control(
            data["control"],
            code="invalid_bind_request_control",
            initial=True,
        )
        with self._control_lock:
            stored_cleanup = self._request_cleanup_receipts.get(data["request_id"])
            if stored_cleanup is not None:
                # Owner cancellation may overtake a generation-0 bind on the
                # independent priority command lane and seal exact cleanup
                # before this ordinary response reaches the node.  The sealed
                # receipt is the monotonic successor of that bind identity:
                # validate it and acknowledge the bind without recreating any
                # request control after cleanup.  Forcing physical fanout to
                # wait for every bind response consumed the cancellation's
                # fixed two-second budget under concurrent admission even
                # though the node already had all immutable identity fields in
                # infer_cancel_wait.
                immutable_fields = _REQUEST_CONTROL_FIELDS - {"cancellation_generation"}
                _require(
                    stored_cleanup.get("complete") is True
                    and all(
                        stored_cleanup.get(field) == control[field]
                        for field in immutable_fields
                    )
                    and stored_cleanup.get("cancellation_generation")
                    == control["cancellation_generation"] + 1,
                    "request_already_cleaned",
                )
                bound_control = {
                    field: stored_cleanup[field] for field in _REQUEST_CONTROL_FIELDS
                }
                return self._signed_result(
                    "request_control_bound",
                    {
                        "request_id": data["request_id"],
                        "control_digest": "sha256:"
                        + hashlib.sha256(
                            canonical_json_bytes(bound_control)
                        ).hexdigest(),
                    },
                )
            current = self._request_controls.get(data["request_id"])
            pending_cancel = self._pending_cancellations.get(data["request_id"])
            bound_control = dict(control)
            if current is not None:
                immutable_fields = _REQUEST_CONTROL_FIELDS - {"cancellation_generation"}
                cancellation_control = self._cancellation_controls.get(
                    data["request_id"]
                )
                advanced_duplicate = (
                    all(current[field] == control[field] for field in immutable_fields)
                    and current["cancellation_generation"]
                    == control["cancellation_generation"] + 1
                    and cancellation_control is not None
                    and all(
                        cancellation_control[field] == current[field]
                        for field in _REQUEST_CONTROL_FIELDS
                    )
                )
                _require(
                    current == control or advanced_duplicate,
                    "conflicting_request_control",
                )
                bound_control = dict(current)
            elif pending_cancel is not None:
                _require(
                    all(
                        pending_cancel[field] == control[field]
                        for field in (
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
                        )
                    )
                    and pending_cancel["cancellation_generation"]
                    == control["cancellation_generation"] + 1,
                    "stale_infer_cancel_generation",
                )
                bound_control["cancellation_generation"] = pending_cancel[
                    "cancellation_generation"
                ]
                self._cancellation_controls[data["request_id"]] = {
                    key: value
                    for key, value in pending_cancel.items()
                    if key != "deadline_budget_ms"
                }
                self._pending_cancellations.pop(data["request_id"], None)
            self._request_controls[data["request_id"]] = bound_control
        return self._signed_result(
            "request_control_bound",
            {
                "request_id": data["request_id"],
                "control_digest": "sha256:"
                + hashlib.sha256(canonical_json_bytes(bound_control)).hexdigest(),
            },
        )

    def _update_request_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(
            payload,
            {"request_id", "control"},
            "invalid_update_request_control_fields",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_update_request_control",
        )
        _require(
            isinstance(data["request_id"], str) and bool(data["request_id"]),
            "invalid_request_id",
        )
        control = self._validated_request_control(
            data["control"],
            code="invalid_update_request_control",
            initial=False,
        )
        with self._control_lock:
            current = self._request_controls.get(data["request_id"])
            immutable = _REQUEST_CONTROL_FIELDS - {"publisher_generation"}
            _require(current is not None, "request_control_unknown")
            _require(
                all(current[field] == control[field] for field in immutable),
                "request_control_identity_mismatch",
            )
            if control["publisher_generation"] == current["publisher_generation"]:
                duplicate = True
            else:
                _require(
                    control["publisher_generation"]
                    == current["publisher_generation"] + 1,
                    "publisher_generation_cas_mismatch",
                )
                duplicate = False
                self._request_controls[data["request_id"]] = dict(control)
        return self._signed_result(
            "request_control_updated",
            {
                "request_id": data["request_id"],
                "publisher_generation": control["publisher_generation"],
                "duplicate": duplicate,
            },
        )

    def _health(self, payload: dict[str, Any]) -> dict[str, Any]:
        _exact_fields(payload, set(), "invalid_health_fields")
        _require(self.state in {"CONFIGURED", "RUNNING"}, "invalid_state_for_health")
        fatal = None if self.transport is None else self.transport.fatal_error
        details: dict[str, Any] = {
            "state": self.state,
            "sidecar_process": (
                None if self.sidecar is None else self.sidecar.status()
            ),
            "transport_fatal_error": (
                None if fatal is None else {"code": fatal.code, "detail": fatal.detail}
            ),
            "transport_running": (
                False if self.transport is None else self.transport.running
            ),
        }
        if self.transport is not None:
            details["transport_counters"] = _plain_json(
                self.transport.counter_snapshot()
            )
        runtime = getattr(self, "runtime", None)
        operation_counters = getattr(runtime, "operation_counter_snapshot", None)
        if callable(operation_counters):
            details["runtime_counters"] = _plain_json(operation_counters())
        return self._signed_result(
            "health",
            details,
        )

    def _snapshot(
        self,
        payload: dict[str, Any],
        *,
        allow_blocking_observations: bool = False,
    ) -> dict[str, Any]:
        """Project one bounded observation for a command or for cleanup sealing.

        ``allow_blocking_observations`` is internal to the cancellation lifecycle
        and is deliberately **not** a payload field, so the validated command
        surface is unchanged.  Round 55 measured the reserved cleanup worker
        proving this exact subject clean in 1.33 ms (owner) and 1.08 ms (peer)
        with the blocking probes, and then burning its whole 1499 ms inline
        budget because the *nonblocking* probes needed a free lock to confirm the
        same fact.  The worker's own proof must therefore be able to seal its
        receipt; the route's fallback snapshots keep the nonblocking behaviour
        exactly as before, and the receipt still requires the same predicates plus
        the same cancellation-generation control proof.
        """

        _require(
            set(payload)
            in (
                set(),
                {"cleanup_subject"},
                {"cleanup_subject", "receipt_only"},
            ),
            "invalid_snapshot_fields",
        )
        cleanup_subject = payload.get("cleanup_subject")
        receipt_only = payload.get("receipt_only", False)
        _require(type(receipt_only) is bool, "invalid_snapshot_fields")
        if cleanup_subject is not None:
            cleanup_subject = _exact_fields(
                cleanup_subject,
                {
                    "deployment_id",
                    "deployment_epoch",
                    "qualification_digest",
                    "request_id",
                    "request_attempt",
                    "path_id",
                    "path_attempt",
                    "path_digest",
                    "topology_generation",
                    "command_id",
                    "cancellation_generation",
                    "publisher_generation",
                    "absolute_deadline_ms",
                },
                "invalid_cleanup_subject",
            )
            _require(
                isinstance(cleanup_subject["deployment_id"], str)
                and bool(cleanup_subject["deployment_id"])
                and type(cleanup_subject["deployment_epoch"]) is int
                and cleanup_subject["deployment_epoch"] >= 1
                and isinstance(cleanup_subject["qualification_digest"], str)
                and cleanup_subject["qualification_digest"].startswith("sha256:")
                and len(cleanup_subject["qualification_digest"]) == 71
                and isinstance(cleanup_subject["request_id"], str)
                and bool(cleanup_subject["request_id"])
                and type(cleanup_subject["request_attempt"]) is int
                and cleanup_subject["request_attempt"] >= 1
                and isinstance(cleanup_subject["path_id"], str)
                and bool(cleanup_subject["path_id"])
                and type(cleanup_subject["path_attempt"]) is int
                and cleanup_subject["path_attempt"] >= 0
                and isinstance(cleanup_subject["path_digest"], str)
                and cleanup_subject["path_digest"].startswith("sha256:")
                and len(cleanup_subject["path_digest"]) == 71
                and type(cleanup_subject["topology_generation"]) is int
                and cleanup_subject["topology_generation"] >= 1
                and isinstance(cleanup_subject["command_id"], str)
                and bool(cleanup_subject["command_id"])
                and type(cleanup_subject["cancellation_generation"]) is int
                and cleanup_subject["cancellation_generation"] >= 0
                and type(cleanup_subject["publisher_generation"]) is int
                and cleanup_subject["publisher_generation"] >= 1
                and type(cleanup_subject["absolute_deadline_ms"]) is int
                and cleanup_subject["absolute_deadline_ms"] > 0,
                "invalid_cleanup_subject",
            )
        _require(self.state in {"CONFIGURED", "RUNNING"}, "invalid_state_for_snapshot")
        assert self.runtime is not None
        if receipt_only and cleanup_subject is not None:
            # A committed receipt is the monotonic successor of every mutable
            # runtime/transport observation for this exact subject.  Check it
            # before touching even the nonblocking probe path: under sustained
            # node work, a fallback command used to spend its entire bounded
            # response attempt observing unrelated state even though the
            # lifecycle worker had already sealed the requested receipt.
            with self._control_lock:
                stored_cleanup = self._request_cleanup_receipts.get(
                    cleanup_subject["request_id"]
                )
                stored_counters = getattr(
                    self,
                    "_request_cleanup_receipt_counters",
                    {},
                ).get(cleanup_subject["request_id"])
                stored_runtime = getattr(
                    self,
                    "_request_cleanup_receipt_runtime",
                    {},
                ).get(cleanup_subject["request_id"])
            if stored_cleanup is not None:
                _require(
                    all(
                        stored_cleanup.get(field) == value
                        for field, value in cleanup_subject.items()
                    ),
                    "cleanup_receipt_identity_mismatch",
                )
                details = {"request_cleanup": dict(stored_cleanup)}
                if isinstance(stored_counters, Mapping):
                    details["transport_counters"] = dict(stored_counters)
                elif self.transport is not None:
                    # Compatibility for a receipt restored or injected without
                    # the co-committed counter projection. Production receipt
                    # sealing always stores the exact counters observed in the
                    # same snapshot below, so the fast path does not reacquire
                    # mutable transport state under load.
                    details["transport_counters"] = _plain_json(
                        self.transport.counter_snapshot()
                    )
                # The route reads per-placement work from the *observed* counters
                # (mycelium_live/route.py:_peer_counters -> _last_snapshots /
                # _last_health) and maps a missing runtime block to
                # applied_operation_count = 0.  A sealed-receipt response without
                # this projection therefore reports zero peer work after every
                # served request, whatever the peer actually did.  The projection
                # is co-committed at seal time, so projecting it reacquires
                # nothing.
                if isinstance(stored_runtime, Mapping):
                    details["runtime"] = dict(stored_runtime)
                return self._signed_result("snapshot", details)
        runtime_observation_complete = True
        runtime_subject_clean: bool | None = None
        if receipt_only and cleanup_subject is not None:
            subject_probe = getattr(self.runtime, "kv_subject_clean", None)
            if callable(subject_probe):
                runtime_subject_clean = bool(
                    subject_probe(
                        cleanup_subject["request_id"],
                        cleanup_subject["path_id"],
                        cleanup_subject["path_attempt"],
                    )
                )
            nonblocking_snapshot = getattr(
                self.runtime, "kv_snapshot_nonblocking", None
            )
            if allow_blocking_observations or not callable(nonblocking_snapshot):
                runtime_snapshot = self.runtime.kv_snapshot()
            else:
                runtime_snapshot = nonblocking_snapshot()
            if runtime_snapshot is None:
                runtime_observation_complete = False
                runtime_snapshot = {
                    "mode": getattr(self.runtime, "decode_mode", None),
                    "states": {},
                    "active_state_count": 1,
                    "active_kv_bytes": 0,
                    "cleanup_observation_pending": True,
                }
        else:
            runtime_snapshot = self.runtime.kv_snapshot()
        details: dict[str, Any] = {
            "runtime": runtime_snapshot,
            "runtime_cleanup_observation_complete": runtime_observation_complete,
        }
        cancellation = self._last_cancellation
        transport_receipt_observation: (
            tuple[Mapping[str, Any], Mapping[str, Any]] | None
        ) = None
        transport_observation_complete = True
        if receipt_only and self.transport is not None:
            nonblocking_transport_observation = getattr(
                self.transport,
                "cancellation_cleanup_observation_nonblocking",
                None,
            )
            if allow_blocking_observations and cleanup_subject is not None:
                # The lifecycle worker owns teardown and has already proven this
                # exact subject clean, so take the same exact-subject projection
                # with the blocking probe rather than reporting an incomplete
                # receipt because a lock happened to be busy.
                transport_receipt_observation = (
                    self.transport.counter_snapshot(),
                    self.transport.cancellation_cleanup_state(
                        cleanup_subject["request_id"],
                        cleanup_subject["path_id"],
                        cleanup_subject["path_attempt"],
                    ),
                )
            elif callable(nonblocking_transport_observation):
                transport_receipt_observation = nonblocking_transport_observation(
                    cleanup_subject["request_id"],
                    cleanup_subject["path_id"],
                    cleanup_subject["path_attempt"],
                )
                if transport_receipt_observation is None:
                    transport_observation_complete = False
                else:
                    details["transport_counters"] = _plain_json(
                        transport_receipt_observation[0]
                    )
            else:
                details["transport_counters"] = _plain_json(
                    self.transport.counter_snapshot()
                )
        details["transport_cleanup_observation_complete"] = (
            transport_observation_complete
        )
        if not receipt_only:
            details.update(
                {
                    "capacity": None
                    if self.capacity is None
                    else _plain_json(self.capacity.snapshot()),
                    "host_resources": self._host_resources(),
                    "transport": None,
                    "sidecar_process": (
                        None if self.sidecar is None else self.sidecar.status()
                    ),
                }
            )
            # A10 live publication: the runtime's own bounded, privacy-reduced
            # stage-compute intervals and runtime batch observations, co-signed
            # with this snapshot.  An unavailable publication is omitted (absent),
            # never zeroed.
            publication = getattr(self.runtime, "a10_publication_snapshot", None)
            if callable(publication):
                try:
                    details["a10_publication"] = _plain_json(publication())
                except Exception:  # noqa: BLE001 - evidence is absent, never fatal
                    pass
            details["interruptibility"] = {
                "runtime_backend": details["runtime"].get("backend"),
                "decode_mode": details["runtime"].get("mode"),
                "work_unit": "transformer_layer",
                "maximum_observed_work_unit_ms": details["runtime"].get(
                    "maximum_observed_work_unit_ms"
                ),
                "observed_work_unit_count": details["runtime"].get(
                    "observed_work_unit_count", 0
                ),
                # wp5-d9 (PLAN D9): the advertised cleanup bound is read from the
                # deadline registry at payload time, never copied. It must fit
                # inside the route's cleanup fanout window
                # (advertised <= route_cleanup_fanout_window_ms), fail closed at
                # startup qualification, never mid-request.  The named constant
                # below is this same registry symbol under the name this module
                # and tests/live/test_cleanup_bound_contract.py resolve, so
                # there is still exactly one owner of the number.
                "maximum_total_cleanup_ms": mycelium_deadlines.advertised_maximum_total_cleanup_ms,
                "physical_proof_required": True,
                "backend_candidate": details["runtime"].get("mode") == "stage_local_kv",
                "cooperative_bound_candidate": (
                    details["runtime"].get("mode") == "stage_local_kv"
                    and type(details["runtime"].get("observed_work_unit_count")) is int
                    and details["runtime"].get("observed_work_unit_count", 0) > 0
                    and isinstance(
                        details["runtime"].get("maximum_observed_work_unit_ms"),
                        (int, float),
                    )
                    and details["runtime"].get("maximum_observed_work_unit_ms", 2_000)
                    < 2_000
                ),
            }
            if self.transport is not None:
                details["transport"] = _plain_json(self.transport.evidence())
                fatal = self.transport.fatal_error
                details["transport_fatal_error"] = (
                    None
                    if fatal is None
                    else {"code": fatal.code, "detail": fatal.detail}
                )
                details["transport_worker_threads"] = (
                    self.transport.worker_threads_alive
                )
                details["transport_dispatcher_phase"] = self.transport.dispatcher_phase
                details["transport_last_dispatch_error"] = (
                    self.transport.last_dispatch_error
                )
                details["transport_outbound_trace"] = list(
                    self.transport.outbound_trace
                )
                details["transport_pending_delivery_count"] = (
                    self.transport.pending_delivery_count
                )
                cancellation = cancellation or self.transport.last_cancellation
                details["transport_cancellation_cleanup_complete"] = (
                    False
                    if cancellation is None
                    else self.transport.cancellation_cleanup_complete(
                        str(cancellation["request_id"]),
                        str(cancellation["path_id"]),
                    )
                )
        if cleanup_subject is not None:
            with self._control_lock:
                stored_cleanup = self._request_cleanup_receipts.get(
                    cleanup_subject["request_id"]
                )
            if stored_cleanup is not None:
                _require(
                    all(
                        stored_cleanup.get(field) == value
                        for field, value in cleanup_subject.items()
                    ),
                    "cleanup_receipt_identity_mismatch",
                )
                details["request_cleanup"] = dict(stored_cleanup)
                return self._signed_result("snapshot", details)
            runtime_states = details["runtime"].get("states", {})
            if runtime_subject_clean is None:
                runtime_subject_clean = runtime_observation_complete and not any(
                    path_id == cleanup_subject["path_id"]
                    and state.get("request_id") == cleanup_subject["request_id"]
                    and state.get("path_attempt") == cleanup_subject["path_attempt"]
                    for path_id, state in runtime_states.items()
                )
            transport_state: Mapping[str, Any] | None = None
            if self.transport is None:
                transport_subject_clean = True
            elif receipt_only and not transport_observation_complete:
                transport_subject_clean = False
            elif transport_receipt_observation is not None:
                transport_state = transport_receipt_observation[1]
                transport_subject_clean = all(
                    value in (0, False)
                    for key, value in transport_state.items()
                    if key != "cancellation_observed"
                )
            else:
                # One lock-bounded transport snapshot owns both the cleanup
                # decision and its signed blocker projection. Calling the
                # boolean probe and state probe separately allowed a racing
                # path lifecycle to produce internally contradictory evidence
                # from two different instants. A receipt must describe exactly
                # one observed state.
                transport_state = self.transport.cancellation_cleanup_state(
                    cleanup_subject["request_id"],
                    cleanup_subject["path_id"],
                    cleanup_subject["path_attempt"],
                )
                transport_subject_clean = all(
                    value in (0, False)
                    for key, value in transport_state.items()
                    if key != "cancellation_observed"
                )
            with self._control_lock:
                cancellation_worker = getattr(self, "_cancellation_workers", {}).get(
                    cleanup_subject["request_id"]
                )
                cancellation_worker_error = getattr(
                    self,
                    "_cancellation_worker_errors",
                    {},
                ).get(cleanup_subject["request_id"])
            cancellation_worker_complete = (
                cancellation_worker is None or not cancellation_worker.is_alive()
            ) and cancellation_worker_error is None
            resource_cleanup_complete = (
                runtime_subject_clean
                and transport_subject_clean
                and cancellation_worker_complete
            )
            details["request_cleanup"] = {
                **cleanup_subject,
                "runtime_clean": runtime_subject_clean,
                "transport_clean": transport_subject_clean,
                "cancellation_worker_complete": cancellation_worker_complete,
                # Resource absence alone cannot prove that the owner-issued
                # cancellation generation was applied.  A receipt becomes
                # complete only after the control proof below also succeeds.
                "complete": False,
            }
            if cancellation_worker_error is not None:
                details["request_cleanup"]["cancellation_worker_error"] = (
                    cancellation_worker_error
                )
            if transport_state is not None:
                details["request_cleanup"]["transport_state"] = dict(transport_state)
            if resource_cleanup_complete:
                with self._control_lock:
                    cancellation_control = self._cancellation_controls.get(
                        cleanup_subject["request_id"]
                    )
                    pending_cancellation = self._pending_cancellations.get(
                        cleanup_subject["request_id"]
                    )
                cancellation_observed = (
                    cleanup_subject["request_id"],
                    cleanup_subject["path_id"],
                    cleanup_subject["path_attempt"],
                ) in self._cancellations_by_subject
                if self.transport is not None:
                    cancellation_observed = (
                        cancellation_observed
                        or self.transport.cancellation_observed(
                            cleanup_subject["request_id"],
                            cleanup_subject["path_id"],
                            cleanup_subject["path_attempt"],
                        )
                    )
                if cancellation_control is not None:
                    cancellation_observed = cancellation_observed or all(
                        cancellation_control.get(field) == value
                        for field, value in cleanup_subject.items()
                    )
                if pending_cancellation is not None:
                    cancellation_observed = cancellation_observed or all(
                        pending_cancellation.get(field) == value
                        for field, value in cleanup_subject.items()
                    )
                with self._control_lock:
                    control = self._request_controls.get(cleanup_subject["request_id"])
                    if control is None:
                        # Cancel-before-start: this node received the owner's
                        # infer_cancel before any infer_start/infer_decode
                        # bound a request control (e.g. cancellation won the
                        # race with the first stage's prefill). The recorded
                        # pending cancellation is the authoritative subject
                        # identity; seed the control from it so the standard
                        # immutable-field and generation verification below
                        # applies exactly as it does for started requests.
                        pending = self._pending_cancellations.get(
                            cleanup_subject["request_id"]
                        )
                        if pending is not None and all(
                            pending.get(field) == value
                            for field, value in cleanup_subject.items()
                            if field != "cancellation_generation"
                        ):
                            control = {
                                key: value
                                for key, value in pending.items()
                                if key != "deadline_budget_ms"
                            }
                            control["cancellation_generation"] = cleanup_subject[
                                "cancellation_generation"
                            ]
                            self._request_controls[cleanup_subject["request_id"]] = (
                                dict(control)
                            )
                    immutable_fields = _REQUEST_CONTROL_FIELDS - {
                        "cancellation_generation"
                    }
                    cancellation_generation_proven = False
                    if (
                        control is not None
                        and cleanup_subject["cancellation_generation"]
                        == control["cancellation_generation"] + 1
                    ):
                        if cancellation_observed:
                            control["cancellation_generation"] = cleanup_subject[
                                "cancellation_generation"
                            ]
                            cancellation_generation_proven = True
                    elif (
                        control is not None
                        and cleanup_subject["cancellation_generation"]
                        == control["cancellation_generation"]
                    ):
                        cancellation_generation_proven = (
                            cleanup_subject["cancellation_generation"] == 0
                            or cancellation_observed
                        )
                    if control is not None:
                        _require(
                            all(
                                cleanup_subject[field] == control[field]
                                for field in immutable_fields
                            ),
                            "cleanup_subject_generation_mismatch",
                        )
                    if cancellation_generation_proven:
                        details["request_cleanup"]["complete"] = True
                        receipt = dict(details["request_cleanup"])
                        if len(self._request_cleanup_receipts) >= 256:
                            oldest_request_id = next(
                                iter(self._request_cleanup_receipts)
                            )
                            self._request_cleanup_receipts.pop(oldest_request_id, None)
                            getattr(
                                self,
                                "_request_cleanup_receipt_counters",
                                {},
                            ).pop(oldest_request_id, None)
                            getattr(
                                self,
                                "_request_cleanup_receipt_runtime",
                                {},
                            ).pop(oldest_request_id, None)
                        self._request_cleanup_receipts[
                            cleanup_subject["request_id"]
                        ] = receipt
                        transport_counters = details.get("transport_counters")
                        if isinstance(transport_counters, Mapping):
                            receipt_counters = getattr(
                                self,
                                "_request_cleanup_receipt_counters",
                                None,
                            )
                            if receipt_counters is None:
                                receipt_counters = {}
                                self._request_cleanup_receipt_counters = (
                                    receipt_counters
                                )
                            receipt_counters[cleanup_subject["request_id"]] = {
                                str(key): int(value)
                                for key, value in transport_counters.items()
                                if type(value) is int
                            }
                        observed_runtime = details.get("runtime")
                        if isinstance(observed_runtime, Mapping):
                            receipt_runtime = getattr(
                                self,
                                "_request_cleanup_receipt_runtime",
                                None,
                            )
                            if receipt_runtime is None:
                                receipt_runtime = {}
                                self._request_cleanup_receipt_runtime = (
                                    receipt_runtime
                                )
                            receipt_runtime[cleanup_subject["request_id"]] = (
                                _plain_json(dict(observed_runtime))
                            )
                        self._request_controls.pop(cleanup_subject["request_id"], None)
                        self._pending_cancellations.pop(
                            cleanup_subject["request_id"], None
                        )
                        self._cancellation_controls.pop(
                            cleanup_subject["request_id"], None
                        )
                if details["request_cleanup"]["complete"] is True:
                    self._sinks.pop(cleanup_subject["request_id"], None)
            # A route fallback snapshot can begin immediately after a pending
            # ``infer_cancel_wait`` response. Both observers may have passed
            # the initial receipt lookup before either commits cleanup. The
            # winner retires request control state; without this final
            # monotonic read, the loser can then sign its stale
            # ``complete=False`` projection even though an authoritative
            # receipt already exists. Always project the committed receipt so
            # cleanup proof cannot move backwards across adjacent observers.
            with self._control_lock:
                committed_cleanup = self._request_cleanup_receipts.get(
                    cleanup_subject["request_id"]
                )
            if committed_cleanup is not None:
                _require(
                    all(
                        committed_cleanup.get(field) == value
                        for field, value in cleanup_subject.items()
                    ),
                    "cleanup_receipt_identity_mismatch",
                )
                details["request_cleanup"] = dict(committed_cleanup)
        return self._signed_result("snapshot", details)

    def _inbound_admission_snapshot(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        fields = {
            "case_id",
            "member_id",
            "spec_digest",
            "source_digest",
            "challenge",
            "expected_endpoint_id",
            "dialed_endpoint_id",
        }
        data = _exact_fields(payload, fields, "invalid_inbound_admission_fields")
        _require(
            self.state == "RUNNING" and self.transport is not None,
            "invalid_state_for_inbound_admission_snapshot",
        )
        _require(
            data["case_id"] == "endpoint_identity_mismatch"
            and all(
                isinstance(data[field], str) and bool(data[field])
                for field in fields
            )
            and data["expected_endpoint_id"] != data["dialed_endpoint_id"]
            and re.fullmatch(r"sha256:[0-9a-f]{64}", data["spec_digest"])
            is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", data["source_digest"])
            is not None
            and re.fullmatch(r"[A-Za-z0-9._:-]{16,256}", data["challenge"])
            is not None,
            "invalid_inbound_admission_identity",
        )
        admission = self.transport.inbound_admission_snapshot(
            data["dialed_endpoint_id"]
        )
        paths = self.transport.evidence().transport_path_observations
        expected_paths = [
            path
            for path in paths
            if path.get("remote_endpoint_id") == data["expected_endpoint_id"]
        ]
        _require(
            all(path.get("path_class") == "unknown" for path in expected_paths),
            "inbound_admission_path_resolved",
        )
        details = {
            "protocol": "mycelium.physical_node.inbound_admission_evidence.v1",
            "case_id": data["case_id"],
            "member_id": data["member_id"],
            "spec_digest": data["spec_digest"],
            "source_digest": data["source_digest"],
            "sidecar_binary_digest": "sha256:"
            + hashlib.sha256(self.sidecar_binary.read_bytes()).hexdigest(),
            "challenge": data["challenge"],
            "expected_endpoint_id": data["expected_endpoint_id"],
            "dialed_endpoint_id": data["dialed_endpoint_id"],
            "expected_peer_path_class": "unknown",
            "admission": dict(admission),
        }
        return self._signed_result("inbound_admission_snapshot", details)

    def _cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(payload, {"request_id"}, "invalid_cancel_fields")
        _require(
            isinstance(data["request_id"], str) and bool(data["request_id"]),
            "invalid_request_id",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_cancel",
        )
        request_id = data["request_id"]
        router = self.router
        assert router is not None
        path_id: str | None = None
        path_attempt: int | None = None
        status_before: str | None = None
        pre_cancel_token_count = 0
        try:
            record = router.get_request(request_id)
            path_id = record.manifest.path_id
            path_attempt = record.manifest.path_attempt
            status_before = record.status
            pre_cancel_token_count = len(record.generated_token_ids)
        except (KeyError, AttributeError):
            pass
        cancelled = router.cancel(request_id)
        try:
            status_after = router.request_status(request_id)
        except KeyError:
            status_after = "UNKNOWN"
        sink = self._sinks.get(request_id)
        observed_token_count = (
            len(sink.token_ids) if sink is not None else pre_cancel_token_count
        )
        post_cancel_token_count = max(0, observed_token_count - pre_cancel_token_count)
        result = {
            "cancelled": bool(cancelled),
            "path_id": path_id,
            "path_attempt": path_attempt,
            "status_before": status_before,
            "status_after": status_after,
            "pre_cancel_token_count": pre_cancel_token_count,
            "post_cancel_token_count": post_cancel_token_count,
        }
        if cancelled and isinstance(path_id, str) and isinstance(path_attempt, int):
            self._last_cancellation = {
                "request_id": request_id,
                "path_id": path_id,
                "path_attempt": path_attempt,
            }
            self._cancellations_by_subject[(request_id, path_id, path_attempt)] = dict(
                self._last_cancellation
            )
        return self._signed_result(
            "cancelled", {"request_id": request_id, "result": _plain_json(result)}
        )

    def _infer_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A11: an optional explicit future-request speculation preference rides
        # beside any of the existing field sets; it never changes those sets.
        speculation = payload.get("speculation")
        payload = {key: value for key, value in payload.items() if key != "speculation"}
        _require(
            set(payload)
            in (
                {"request"},
                {"request", "excluded_placement_ids"},
                {"request", "control", "path_manifest"},
                {
                    "request",
                    "control",
                    "path_manifest",
                    "excluded_placement_ids",
                },
            ),
            "invalid_infer_start_fields",
        )
        data = payload
        request_data = _exact_fields(
            data["request"],
            {
                "request_id",
                "prompt_token_ids",
                "max_new_tokens",
                "expected_new_tokens",
                "qos_class",
                "admitted_at",
                "target_ttft_ms",
                "target_tpot_ms",
                "target_tokens_per_second",
                "sampling_seed",
                "generation_config_digest",
            },
            "invalid_request_fields",
        )
        _require(
            isinstance(request_data["prompt_token_ids"], list),
            "invalid_prompt_token_ids",
        )
        request = RequestContext(
            **{
                **request_data,
                "prompt_token_ids": tuple(request_data["prompt_token_ids"]),
                "admitted_at": self._clock.now(),
            }
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_infer_start",
        )
        excluded_placement_ids = data.get("excluded_placement_ids", [])
        _require(
            isinstance(excluded_placement_ids, list)
            and all(
                isinstance(item, str) and bool(item) for item in excluded_placement_ids
            )
            and len(excluded_placement_ids) == len(set(excluded_placement_ids)),
            "invalid_excluded_placement_ids",
        )
        _require(request.request_id not in self._sinks, "duplicate_request_id")
        if speculation is not None:
            speculation = _exact_fields(
                speculation, {"preference"}, "invalid_infer_start_speculation"
            )
            _require(
                speculation["preference"] in {"target_only", "speculative_requested"},
                "invalid_infer_start_speculation",
            )
            a11_wiring = getattr(self, "_a11_wiring", None)
            if a11_wiring is not None:
                # Without a staged draft the request simply runs target-only: a
                # client cannot force an unavailable overlay.
                a11_wiring.set_preference(request.request_id, speculation["preference"])
        control_data = data.get("control")
        if control_data is not None:
            control = self._validated_request_control(
                control_data,
                code="invalid_infer_start_control",
                initial=True,
            )
            with self._control_lock:
                current = self._request_controls.get(request.request_id)
                immutable_fields = _REQUEST_CONTROL_FIELDS - {"cancellation_generation"}
                cancellation_preceded_start = bool(
                    current is not None
                    and current["cancellation_generation"]
                    == control["cancellation_generation"] + 1
                    and all(
                        current[field] == control[field] for field in immutable_fields
                    )
                )
                _require(
                    current is None
                    or current == control
                    or cancellation_preceded_start,
                    "conflicting_request_control",
                )
                if not cancellation_preceded_start:
                    self._request_controls[request.request_id] = dict(control)
                pending_cancel = self._pending_cancellations.pop(
                    request.request_id, None
                )
                if cancellation_preceded_start or pending_cancel is not None:
                    if pending_cancel is None:
                        pending_cancel = current
                    assert pending_cancel is not None
                    _require(
                        all(
                            pending_cancel[field] == control[field]
                            for field in (
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
                            )
                        )
                        and pending_cancel["cancellation_generation"] == 1,
                        "stale_infer_cancel_generation",
                    )
                    self._request_controls[request.request_id][
                        "cancellation_generation"
                    ] = 1
                    cancellation_controls = getattr(
                        self,
                        "_cancellation_controls",
                        None,
                    )
                    if cancellation_controls is None:
                        cancellation_controls = {}
                        self._cancellation_controls = cancellation_controls
                    cancellation_controls[request.request_id] = {
                        key: value
                        for key, value in pending_cancel.items()
                        if key != "deadline_budget_ms"
                    }
                    return self._signed_result(
                        "inference_started",
                        {
                            "request_id": request.request_id,
                            "status": "CANCELLED",
                            "output": {"token_indexes": [], "token_ids": []},
                            "path": None,
                        },
                    )
        sink = _CaptureSink()
        # Register the wait's deadline with the transport *before* the first
        # chunk leaves, so every hop of this request is ordered inside it.
        budget = _PrefillWaitBudget(
            transport=getattr(self, "transport", None),
            request_id=request.request_id,
            prompt_tokens=len(request.prompt_token_ids),
            chunk_size_tokens=getattr(
                getattr(self, "_router_config", None),
                "prefill_chunk_size_tokens",
                0,
            ),
            graph=getattr(self, "graph", None),
            command_timeout=self.command_timeout,
        )
        try:
            return self._infer_start_and_wait(
                request,
                sink,
                budget=budget,
                control=control if control_data is not None else None,
                control_data=control_data,
                data=data,
                excluded_placement_ids=excluded_placement_ids,
            )
        finally:
            budget.release()

    def _infer_start_and_wait(
        self,
        request: RequestContext,
        sink: "_CaptureSink",
        *,
        budget: _PrefillWaitBudget,
        control: Any,
        control_data: Any,
        data: Mapping[str, Any],
        excluded_placement_ids: list[str],
    ) -> dict[str, Any]:
        assert self.router is not None
        if control_data is None:
            request_id = self.router.start_distributed_prefill(
                request,
                sink,
                excluded_placements=frozenset(excluded_placement_ids),
            )
        else:
            _require(
                not excluded_placement_ids, "locked_path_cannot_exclude_placements"
            )
            manifest_data = data.get("path_manifest")
            _require(isinstance(manifest_data, dict), "invalid_path_manifest")
            try:
                manifest = path_manifest_from_dict(manifest_data)
                _require(self.graph is not None, "missing_execution_graph")
                validate_manifest(manifest, self.graph)
            except (TypeError, ValueError):
                raise NodeCommandError("invalid_path_manifest") from None
            manifest_digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        manifest_data,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
            _require(
                manifest.request_id == request.request_id
                and manifest.path_id == control["path_id"]
                and manifest.path_attempt == control["path_attempt"]
                and manifest_digest == control["path_digest"],
                "path_manifest_identity_mismatch",
            )
            request_id = self.router.start_locked_distributed_prefill(
                request,
                sink,
                manifest=manifest,
                pinned_deployment=self.graph,
            )
        _require(request_id == request.request_id, "request_id_changed")
        self._sinks[request_id] = sink
        # A14 class D: the wait follows the request's link progress instead
        # of a fixed number.  It ends with the transport's own verdict for the
        # request (e.g. ``prefill_transport_stalled`` after its bounded
        # resends, or ``delivery_deadline_exceeded``), with
        # ``prefill_transport_stalled`` when nothing progressed for the
        # watchdog, and otherwise at the deadline that scales with the bytes
        # still to move.
        status = self.router.request_status(request_id)
        failure: str | None = None
        while status in {"PREFILL", "LOCKED"}:
            if self.transport is not None and self.transport.fatal_error is not None:
                raise NodeCommandError(self.transport.fatal_error.code)
            failure = budget.observe()
            if failure is not None:
                break
            if budget.stalled():
                failure = "prefill_transport_stalled"
                break
            if time.monotonic() >= budget.deadline:
                failure = budget.expiry_code()
                break
            time.sleep(0.01)
            status = self.router.request_status(request_id)
        if status in {"PREFILL", "LOCKED"}:
            # Report the transport's own bounded verdict and the wait's link
            # figures before raising.
            code = failure or "prefill_completion_timeout"
            _emit_prefill_transport_diagnostic(self.transport)
            budget.emit_diagnostic(code)
            # The stalled state itself - every thread's stack, who holds the
            # data client, what is outstanding - before the failure unwinds it.
            prefill = None
            try:
                prefill = budget.snapshot()
            except Exception:
                pass
            self.write_evidence_bundle(code, request_id=request_id, prefill=prefill)
            raise NodeCommandError(code)
        deadline = budget.deadline
        self._require_started_status(
            request_id,
            status,
            legacy=control_data is None,
        )
        if getattr(self.runtime, "decode_mode", None) == "stage_local_kv":
            # PrefillChunkCompleted and the first TokenEvent are separately
            # ordered physical frames. The former may transition the entry to
            # DECODING just before the latter reaches its sink. Stage-local KV
            # cannot dispatch decode until that first generated token exists,
            # so wait for the correlated event instead of issuing an invalid
            # early decode command.
            while (
                status == "DECODING"
                and not sink.token_ids
                and time.monotonic() < deadline
            ):
                if (
                    self.transport is not None
                    and self.transport.fatal_error is not None
                ):
                    raise NodeCommandError(self.transport.fatal_error.code)
                time.sleep(0.01)
                status = self.router.request_status(request_id)
            if status == "CANCELLED":
                # Owner cancellation can overtake the separately ordered
                # first TokenEvent after prefill has already transitioned the
                # Router to DECODING.  That is the same valid cancellation
                # terminal as cancel-before-start, not a missing-token
                # failure.  Do not dereference the Router record here: exact
                # generation-fenced teardown may already have retired it.
                return self._signed_result(
                    "inference_started",
                    {
                        "request_id": request_id,
                        "status": "CANCELLED",
                        "output": sink.snapshot(),
                        "path": None,
                    },
                )
            self._require_started_status(
                request_id,
                status,
                legacy=control_data is None,
            )
            _require(
                bool(sink.token_ids) or status == "COMPLETED",
                "prefill_token_timeout",
            )
        record = self.router.get_request(request_id)
        manifest = record.manifest
        if control_data is None and status in {"COMPLETED", "FAILED", "CANCELLED"}:
            self._retire_legacy_terminal_path(request_id)
        return self._signed_result(
            "inference_started",
            {
                "request_id": request_id,
                "status": status,
                "output": sink.snapshot(),
                "path": {
                    "path_id": manifest.path_id,
                    "path_attempt": manifest.path_attempt,
                    "placement_ids": [
                        hop.placement_id for hop in manifest.ordered_hops
                    ],
                },
            },
        )

    def _require_started_status(
        self,
        request_id: str,
        status: str,
        *,
        legacy: bool,
    ) -> None:
        """Fail closed when prefill ended the request without a live result.

        ``inference_started`` is a success-shaped, signed observation.  Only a
        request that is decoding, already completed, or cancelled by its owner
        may be reported through it.  A terminal ``FAILED`` (or any other
        status) means the path executed no usable work, so returning it would
        let a caller mistake an empty output for a completed request.  The
        legacy qualification path still retires its terminal Router state
        before the error is raised.
        """

        if status in _STARTED_REQUEST_STATUSES:
            return
        cause = None
        reader = getattr(getattr(getattr(self, "router", None), "entry", None), "last_failure_report", None)
        if callable(reader):
            try:
                cause = reader(request_id)
            except Exception:
                cause = None
        _print_inference_start_failure(status, cause)
        if legacy and status in {"COMPLETED", "FAILED", "CANCELLED"}:
            self._retire_legacy_terminal_path(request_id)
        raise NodeCommandError("inference_start_failed")

    def _infer_decode(self, payload: dict[str, Any]) -> dict[str, Any]:
        fields = set(payload)
        _require(
            fields
            in (
                {"request_id", "count"},
                {"request_id", "count", "control"},
            ),
            "invalid_infer_decode_fields",
        )
        data = payload
        _require(
            isinstance(data["request_id"], str) and bool(data["request_id"]),
            "invalid_request_id",
        )
        _require(
            isinstance(data["count"], int)
            and not isinstance(data["count"], bool)
            and 1 <= data["count"] <= 128,
            "invalid_decode_count",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_infer_decode",
        )
        request_id = data["request_id"]
        command_control: dict[str, Any] | None = None
        owner_cancellation_overtook_decode = False
        if "control" in data:
            control = _exact_fields(
                data["control"],
                {
                    "deployment_id",
                    "deployment_epoch",
                    "qualification_digest",
                    "request_attempt",
                    "path_id",
                    "path_attempt",
                    "path_digest",
                    "topology_generation",
                    "command_id",
                    "cancellation_generation",
                    "publisher_generation",
                    "absolute_deadline_ms",
                },
                "invalid_infer_decode_control",
            )
            with self._control_lock:
                current = self._request_controls.get(request_id)
                owner_control: object = current
                if current is None:
                    receipt = self._request_cleanup_receipts.get(request_id)
                    if (
                        isinstance(receipt, Mapping)
                        and receipt.get("complete") is True
                    ):
                        owner_control = receipt
                owner_cancellation_overtook_decode = (
                    _is_exact_owner_cancellation_successor(control, owner_control)
                )
                _require(
                    current == control or owner_cancellation_overtook_decode,
                    "stale_infer_decode_generation",
                )
            command_control = dict(control)
        sink = self._sinks.get(request_id)
        if sink is None:
            _require(
                owner_cancellation_overtook_decode,
                "unknown_request_id",
            )

        def cancelled_decode_result() -> dict[str, Any]:
            # Owner cancellation can advance before a queued decode enters the
            # inference lane, while it is inside transport dispatch, or just
            # before its terminal status read.  In each ordering, wait only
            # inside this command's existing completion budget for the Router
            # teardown that the exact successor generation authorized.
            deadline = time.monotonic() + min(
                self.command_timeout * 0.8,
                _MAX_INFERENCE_COMPLETION_TIMEOUT_SECONDS,
            )
            while True:
                try:
                    status = self.router.request_status(request_id)
                except KeyError:
                    status = "CANCELLED"
                if status in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                _require(
                    time.monotonic() < deadline,
                    "decode_cancellation_completion_timeout",
                )
                time.sleep(0.01)
            return self._signed_result(
                "inference_decoded",
                {
                    "request_id": request_id,
                    "dispatched": 0,
                    "status": status,
                    "output": (
                        sink.snapshot()
                        if sink is not None
                        else {"token_indexes": [], "token_ids": []}
                    ),
                },
            )

        if owner_cancellation_overtook_decode:
            return cancelled_decode_result()

        assert sink is not None
        dispatched = 0
        a11_wiring = getattr(self, "_a11_wiring", None)
        decode_step = (
            self.router.decode_one_distributed
            if a11_wiring is None
            else self.router.speculate_one_distributed
        )
        for _ in range(data["count"]):
            output_count = len(sink.token_ids)
            round_started = time.monotonic()
            try:
                decode_dispatched = decode_step(data["request_id"])
            except IrohTransportError as exc:
                if exc.code != "path_cancelled" or command_control is None:
                    raise
                with self._control_lock:
                    current = self._request_controls.get(request_id)
                    owner_control = current
                    if current is None:
                        receipt = self._request_cleanup_receipts.get(request_id)
                        if (
                            isinstance(receipt, Mapping)
                            and receipt.get("complete") is True
                        ):
                            owner_control = receipt
                    owner_cancellation_overtook_decode = (
                        _is_exact_owner_cancellation_successor(
                            command_control,
                            owner_control,
                        )
                    )
                if not owner_cancellation_overtook_decode:
                    raise
                return cancelled_decode_result()
            if not decode_dispatched:
                break
            dispatched += 1
            deadline = time.monotonic() + min(
                self.command_timeout * 0.8,
                _MAX_INFERENCE_COMPLETION_TIMEOUT_SECONDS,
            )
            status = self.router.request_status(data["request_id"])
            while (
                len(sink.token_ids) == output_count
                and status not in {"COMPLETED", "FAILED", "CANCELLED"}
                and time.monotonic() < deadline
            ):
                if (
                    self.transport is not None
                    and self.transport.fatal_error is not None
                ):
                    raise NodeCommandError(self.transport.fatal_error.code)
                time.sleep(0.01)
                status = self.router.request_status(data["request_id"])
            if not (
                len(sink.token_ids) > output_count
                or status in {"COMPLETED", "CANCELLED", "FAILED"}
            ):
                # The wait above observes only transport.fatal_error, exactly like
                # the prefill wait -- so a token hop that failed in the deferred
                # forward path leaves no visible cause before the generic raise
                # below.  Report the transport's own bounded verdict first.  This
                # is the stage that stalled for the whole native budget in round37
                # while the owner recorded an opaque reconciliation_required.
                _emit_prefill_transport_diagnostic(self.transport)
            _require(
                len(sink.token_ids) > output_count
                or status in {"COMPLETED", "CANCELLED", "FAILED"},
                "decode_completion_timeout",
            )
            if a11_wiring is not None and len(sink.token_ids) > output_count:
                a11_wiring.note_decode_round(
                    request_id,
                    elapsed_ms=(time.monotonic() - round_started) * 1000.0,
                    tokens=len(sink.token_ids) - output_count,
                )
        terminal_status = self.router.request_status(data["request_id"])
        if command_control is None and terminal_status in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        }:
            self._retire_legacy_terminal_path(data["request_id"])
        return self._signed_result(
            "inference_decoded",
            {
                "request_id": data["request_id"],
                "dispatched": dispatched,
                "status": terminal_status,
                "output": sink.snapshot(),
            },
        )

    def _retire_legacy_terminal_path(self, request_id: str) -> None:
        """Retire a completed legacy qualification path before acknowledging it.

        Product inference owns generation-fenced cleanup through
        ``infer_cancel_wait`` on every participant. The startup qualifier is the
        sole legacy caller: it has no product control identity, but its terminal
        Router record still contains the exact request/path attempt needed to
        release remote relay state and mirrored capacity. Dispatch that existing
        entry-authoritative teardown and wait for its delivery receipt before the
        legacy command returns. This prevents route shutdown from racing the
        asynchronous cancellation worker and leaving persistent capacity behind.
        """

        router = self.router
        transport = self.transport
        _require(
            router is not None and transport is not None,
            "legacy_cleanup_unavailable",
        )
        try:
            record = router.get_request(request_id)
        except KeyError:
            return
        _require(
            record.status in {"COMPLETED", "FAILED", "CANCELLED"},
            "legacy_cleanup_before_terminal",
        )
        manifest = record.manifest
        cancellation = PathCancellation(
            request_id=request_id,
            path_id=manifest.path_id,
            path_attempt=manifest.path_attempt,
            topology_version=manifest.topology_version,
        )
        transport.send_path_cancellation_if_entry(cancellation)
        deadline = time.monotonic() + min(_MAX_LEGACY_TERMINAL_CLEANUP_SECONDS, self.command_timeout * 0.8)
        while not transport.cancellation_cleanup_complete(
            request_id,
            manifest.path_id,
            manifest.path_attempt,
        ):
            fatal = transport.fatal_error
            if fatal is not None:
                raise NodeCommandError(fatal.code)
            _require(
                time.monotonic() < deadline,
                "legacy_terminal_cleanup_timeout",
            )
            time.sleep(0.01)

    def _apply_generation_fenced_cancellation(self, data: Mapping[str, Any]) -> None:
        transport = getattr(self, "transport", None)
        deadline_budget_ms = data.get("deadline_budget_ms")
        cleanup_deadline_monotonic_s = (
            None
            if type(deadline_budget_ms) is not int
            else time.monotonic()
            + max(
                0.0,
                deadline_budget_ms / 1_000.0 - _CANCEL_RECEIPT_FALLBACK_RESERVE_SECONDS,
            )
        )
        # Fence model execution before touching Router/relay ownership. Entry
        # record teardown and participant path teardown may briefly wait on
        # their own subject locks; the runtime marker must already be visible
        # so active MLX/NumPy work exits at its next per-layer checkpoint.
        # Repeating runtime.cancel later through relay release is idempotent.
        runtime_cancel = getattr(getattr(self, "runtime", None), "cancel", None)
        request_key = str(data["request_id"])
        call_index = 1
        try:
            with self._control_lock:
                counts = getattr(self, "_cancel_phase_counts", None)
                if counts is None:
                    counts = {}
                    self._cancel_phase_counts = counts
                call_index = counts.get(request_key, 0) + 1
                counts[request_key] = call_index
        except BaseException:
            call_index = 1
        call_started = time.monotonic()
        phase_started = call_started
        if callable(runtime_cancel):
            runtime_cancel(data["path_id"])
        phase_now = time.monotonic()
        _cancel_phase_diagnostic(
            "runtime_marker",
            (phase_now - phase_started) * 1_000.0,
            call_index=call_index,
        )
        phase_started = phase_now
        controlled_cancellation = PathCancellation(
            request_id=data["request_id"],
            path_id=data["path_id"],
            path_attempt=data["path_attempt"],
            topology_version=data["topology_generation"],
        )
        # Fence and interrupt exact-subject transport work before Router
        # teardown. A send can hold RelayEngine's per-path operation lock;
        # calling cancel_local first would then wait behind the very send that
        # controlled transport cancellation is responsible for interrupting.
        # This ordering is runtime fence -> transport fence -> Router release.
        apply_controlled = getattr(
            transport,
            "apply_controlled_path_cancellation",
            None,
        )
        transport_cancelled = False
        if callable(apply_controlled):
            transport_cancelled = bool(
                apply_controlled(
                    controlled_cancellation,
                    entry_cancelled=False,
                    cleanup_deadline_monotonic_s=cleanup_deadline_monotonic_s,
                )
            )
        phase_now = time.monotonic()
        _cancel_phase_diagnostic(
            "transport_fence",
            (phase_now - phase_started) * 1_000.0,
            call_index=call_index,
        )
        phase_started = phase_now
        cancel_local = getattr(self.router, "cancel_local", None)
        if callable(cancel_local):
            router_cancelled = bool(cancel_local(data["request_id"]))
            cancelled = transport_cancelled or router_cancelled
        else:
            cancelled = bool(self.router.cancel(data["request_id"]))
        phase_now = time.monotonic()
        _cancel_phase_diagnostic(
            "router_cancel_local",
            (phase_now - phase_started) * 1_000.0,
            call_index=call_index,
        )
        if not cancelled and transport is not None:
            transport.send_path_cancellation_if_entry(controlled_cancellation)
        _cancel_phase_diagnostic(
            "total",
            (time.monotonic() - call_started) * 1_000.0,
            call_index=call_index,
        )

    @staticmethod
    def _cancellation_worker_error_code(error: BaseException) -> str:
        """Return a bounded non-payload diagnostic for fail-closed receipts."""

        error_type = type(error).__name__
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not code:
            code = "unclassified"
        return f"{error_type}:{code}"[:128]

    def _cancellation_resources_clean(self, data: Mapping[str, Any]) -> bool:
        """Return whether the exact runtime and transport subject is released.

        This deliberately excludes the cancellation worker marker: this helper is
        called by that worker while it still owns teardown.  Router teardown is
        synchronous; the two lower layers are the ones that can finish or fail
        asynchronously after a sweep was successfully dispatched.
        """

        request_id = str(data["request_id"])
        path_id = str(data["path_id"])
        path_attempt = int(data["path_attempt"])

        runtime_clean = True
        runtime_subject_clean = getattr(
            getattr(self, "runtime", None),
            "kv_subject_clean",
            None,
        )
        if callable(runtime_subject_clean):
            runtime_clean = bool(
                runtime_subject_clean(request_id, path_id, path_attempt)
            )

        transport_clean = True
        transport_subject_clean = getattr(
            getattr(self, "transport", None),
            "cancellation_cleanup_complete",
            None,
        )
        if callable(transport_subject_clean):
            transport_clean = bool(
                transport_subject_clean(request_id, path_id, path_attempt)
            )

        return runtime_clean and transport_clean

    def _apply_cancellation_until_deadline(self, data: Mapping[str, Any]) -> None:
        """Retry one idempotent teardown inside the original owner budget.

        The cancellation fence is monotonic and every release operation is
        idempotent.  Retrying the same exact identity is therefore the correct
        recovery when a transient lower-layer release raises after publishing
        only part of teardown *or* when asynchronous delivery cancellation
        returns from dispatch before the exact subject is clean.  The retry
        window is derived from the aged ``deadline_budget_ms`` received from the
        owner and preserves the same fallback reserve used by
        ``infer_cancel_wait``; it never extends the frozen two-second protocol
        deadline.
        """

        request_id = str(data["request_id"])
        deadline_budget_ms = data.get("deadline_budget_ms")
        retry_deadline = (
            time.monotonic()
            if type(deadline_budget_ms) is not int
            else time.monotonic()
            + max(
                0.0,
                deadline_budget_ms / 1_000.0 - _CANCEL_RECEIPT_FALLBACK_RESERVE_SECONDS,
            )
        )
        attempt = dict(data)
        while True:
            try:
                self._apply_generation_fenced_cancellation(attempt)
                with self._control_lock:
                    getattr(self, "_cancellation_worker_errors", {}).pop(
                        request_id,
                        None,
                    )
                if self._cancellation_resources_clean(attempt):
                    return
            except Exception as error:
                with self._control_lock:
                    errors = getattr(self, "_cancellation_worker_errors", None)
                    if errors is None:
                        errors = {}
                        self._cancellation_worker_errors = errors
                    errors[request_id] = self._cancellation_worker_error_code(error)
                    while len(errors) > 256:
                        errors.pop(next(iter(errors)), None)
            remaining = retry_deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(_CANCEL_RECEIPT_POLL_SECONDS, remaining))
            # ``_apply_generation_fenced_cancellation`` passes an absolute
            # deadline to transport after subtracting the fixed fallback
            # reserve.  Re-age the duration so every retry observes the same
            # original deadline rather than receiving a fresh one.
            remaining = max(0.0, retry_deadline - time.monotonic())
            attempt["deadline_budget_ms"] = max(
                1,
                int((remaining + _CANCEL_RECEIPT_FALLBACK_RESERVE_SECONDS) * 1_000),
            )

    def _seal_cancellation_receipt_until_deadline(
        self,
        data: Mapping[str, Any],
        *,
        deadline_monotonic_s: float,
    ) -> None:
        """Commit exact cleanup proof from the teardown lifecycle itself.

        A route snapshot remains a fail-closed fallback, but it must not be the
        only component capable of turning completed physical teardown into a
        receipt.  Under concurrent cancellation that ordering produced a
        positive-feedback loop: every owner observed the worker as pending,
        queued another snapshot, and exhausted the response lane before any
        snapshot could commit the already-clean subject.  The lifecycle worker
        owns the exact identity and original deadline, so it seals the same
        receipt as soon as its worker marker has been retired.
        """

        # Several direct unit fixtures exercise only generation fencing and do
        # not construct a runtime.  A configured physical service always has
        # one; leaving those narrow fixtures on the explicit snapshot path does
        # not alter production behavior.
        if getattr(self, "runtime", None) is None:
            return
        cleanup_subject = {
            key: value for key, value in data.items() if key != "deadline_budget_ms"
        }
        while True:
            try:
                observation = self._snapshot(
                    {
                        "cleanup_subject": cleanup_subject,
                        "receipt_only": True,
                    },
                    # This worker has already proven the exact subject clean; its
                    # own sealing must not wait for a nonblocking probe to find a
                    # free lock (round 55: teardown 1.33 ms, seal 1499 ms).
                    allow_blocking_observations=True,
                )
            except Exception as error:
                # Snapshot validation remains fail closed.  A transient
                # nonblocking observation can be retried only inside the
                # immutable owner deadline; the route's independent observer
                # retains the same authority and diagnostic surface.
                observation = None
                _cancel_phase_diagnostic(
                    "seal_poll_error",
                    0.0,
                    call_index=0,
                    code=(
                        getattr(error, "code", None)
                        or type(error).__name__
                    ),
                )
            details = (
                observation.get("details") if isinstance(observation, Mapping) else None
            )
            receipt = (
                details.get("request_cleanup") if isinstance(details, Mapping) else None
            )
            # ``_snapshot`` returns a *signed envelope* in production
            # (``result.observation.details.request_cleanup``), while the
            # documented unit doubles flatten it to ``details.request_cleanup``.
            # Reading only the flattened shape made this loop blind on real
            # hardware: it observed "no receipt" forever, slept one poll cadence
            # and ran out the whole inline budget -- measured 2026-09-15 as a
            # deterministic 1.5 s stall on both nodes (owner handler 1.62-1.71 s
            # against a 1499 ms budget) even though the receipt was already
            # committed and complete.  The lifecycle worker therefore reads the
            # receipt it just caused to be committed, which is exactly what its
            # caller checks, and which cannot depend on an envelope shape.
            with self._control_lock:
                committed = self._request_cleanup_receipts.get(
                    cleanup_subject["request_id"]
                )
            committed_complete = (
                isinstance(committed, Mapping)
                and committed.get("complete") is True
                and all(
                    committed.get(field) == value
                    for field, value in cleanup_subject.items()
                )
            )
            _cancel_phase_diagnostic(
                "seal_poll",
                0.0,
                call_index=0,
                code=(
                    "committed_complete"
                    if committed_complete
                    else "committed_pending"
                    if isinstance(committed, Mapping)
                    else "envelope_"
                    + ("complete" if receipt is None else str(receipt.get("complete")))
                ),
            )
            if committed_complete:
                return
            remaining = deadline_monotonic_s - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(_CANCEL_RECEIPT_POLL_SECONDS, remaining))

    def _infer_cancel(
        self,
        payload: dict[str, Any],
        *,
        defer_cleanup: bool = True,
    ) -> dict[str, Any]:
        fields = set(payload)
        _require(
            fields
            in (
                {"request_id"},
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
                },
            ),
            "invalid_infer_cancel_fields",
        )
        data = payload
        _require(
            isinstance(data["request_id"], str) and bool(data["request_id"]),
            "invalid_request_id",
        )
        _require(
            self.state == "RUNNING" and self.router is not None,
            "invalid_state_for_infer_cancel",
        )
        pending_start = False
        if len(fields) > 1:
            cancel_control = self._validated_request_control(
                {field: data[field] for field in _REQUEST_CONTROL_FIELDS},
                code="invalid_infer_cancel_control",
                initial=False,
            )
            _require(
                cancel_control["cancellation_generation"] >= 1
                and type(data["deadline_budget_ms"]) is int
                and 1 <= data["deadline_budget_ms"]
                <= mycelium_deadlines.COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS,
                "invalid_infer_cancel_control",
            )
            with self._control_lock:
                control = self._request_controls.get(data["request_id"])
                if control is None:
                    previous = self._pending_cancellations.get(data["request_id"])
                    _require(
                        previous is None or previous == dict(data),
                        "conflicting_pending_cancellation",
                    )
                    self._pending_cancellations[data["request_id"]] = dict(data)
                    pending_start = True
                else:
                    _require(
                        all(
                            data[field] == control[field]
                            for field in (
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
                            )
                        )
                        and data["cancellation_generation"]
                        == control["cancellation_generation"] + 1,
                        "stale_infer_cancel_generation",
                    )
                    control["cancellation_generation"] = data["cancellation_generation"]
        if len(fields) > 1:
            with self._control_lock:
                cancellation_controls = getattr(
                    self,
                    "_cancellation_controls",
                    None,
                )
                if cancellation_controls is None:
                    cancellation_controls = {}
                    self._cancellation_controls = cancellation_controls
                cancellation_controls[data["request_id"]] = {
                    key: value
                    for key, value in data.items()
                    if key != "deadline_budget_ms"
                }
            if not defer_cleanup:
                # ``infer_cancel_wait`` already owns a reserved control-lane
                # worker and cannot acknowledge until its ordered receipt
                # attempt is complete. Spawning a detached lifecycle worker
                # here lets this command publish ``cleanup_pending`` before the
                # only component capable of sealing its receipt has finished.
                # Under concurrent cancellation every owner then queues
                # fallback snapshots over the same remote control channel,
                # even though teardown is already complete locally. Apply and
                # retry the generation fence in this reserved worker instead,
                # and keep it registered for the whole teardown. A fallback
                # snapshot can still run concurrently on another reserved
                # worker, but cannot seal a transiently clean observation.
                with self._control_lock:
                    workers = getattr(self, "_cancellation_workers", None)
                    if workers is None:
                        workers = {}
                        self._cancellation_workers = workers
                    existing = workers.get(data["request_id"])
                    _require(
                        existing is None or not existing.is_alive(),
                        "duplicate_infer_cancel_worker",
                    )
                    inline_worker = threading.current_thread()
                    workers[data["request_id"]] = inline_worker
                try:
                    self._apply_cancellation_until_deadline(dict(data))
                finally:
                    with self._control_lock:
                        current = self._cancellation_workers.get(data["request_id"])
                        if current is inline_worker:
                            self._cancellation_workers.pop(data["request_id"], None)
                return self._signed_result(
                    "inference_cancelled",
                    {
                        "request_id": data["request_id"],
                        "cancelled": True,
                        "status": "CANCELLING",
                        "cleanup_pending": True,
                        **({"pending_start": True} if pending_start else {}),
                    },
                )
            with self._control_lock:
                workers = getattr(self, "_cancellation_workers", None)
                if workers is None:
                    workers = {}
                    self._cancellation_workers = workers
                _require(
                    data["request_id"] not in workers,
                    "duplicate_infer_cancel_worker",
                )
                receipt_events = getattr(
                    self,
                    "_cancellation_receipt_events",
                    None,
                )
                if receipt_events is None:
                    receipt_events = {}
                    self._cancellation_receipt_events = receipt_events
                receipt_event = threading.Event()
                receipt_events[data["request_id"]] = receipt_event
                receipt_deadline_monotonic_s = time.monotonic() + (
                    data["deadline_budget_ms"] / 1_000.0
                )

                def apply_cancellation() -> None:
                    try:
                        self._apply_cancellation_until_deadline(dict(data))
                    finally:
                        with self._control_lock:
                            current = self._cancellation_workers.get(data["request_id"])
                            if current is threading.current_thread():
                                self._cancellation_workers.pop(data["request_id"], None)
                    try:
                        self._seal_cancellation_receipt_until_deadline(
                            dict(data),
                            deadline_monotonic_s=receipt_deadline_monotonic_s,
                        )
                    finally:
                        receipt_event.set()
                        with self._control_lock:
                            current_event = self._cancellation_receipt_events.get(
                                data["request_id"]
                            )
                            if current_event is receipt_event:
                                self._cancellation_receipt_events.pop(
                                    data["request_id"], None
                                )

                worker = threading.Thread(
                    target=apply_cancellation,
                    name=f"mycelium-cancel-{data['request_id'][:12]}",
                    daemon=True,
                )
                workers[data["request_id"]] = worker
                try:
                    worker.start()
                except BaseException:
                    workers.pop(data["request_id"], None)
                    raise
            return self._signed_result(
                "inference_cancelled",
                {
                    "request_id": data["request_id"],
                    "cancelled": True,
                    "status": "CANCELLING",
                    "cleanup_pending": True,
                    **({"pending_start": True} if pending_start else {}),
                },
            )

        cancelled = self.router.cancel(data["request_id"])
        try:
            status = self.router.request_status(data["request_id"])
        except KeyError:
            # Successful Router cleanup may retire the request before this
            # command builds its observation.  Absence after the exact,
            # generation-fenced cancellation is the expected cancelled state;
            # cleanup snapshots still prove every request/path-scoped resource.
            status = "CANCELLED"
        return self._signed_result(
            "inference_cancelled",
            {
                "request_id": data["request_id"],
                "cancelled": bool(cancelled),
                "status": status,
            },
        )

    def _infer_cancel_wait(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Fence cancellation and return the ordered lifecycle receipt.

        This reserved worker owns generation fencing, teardown, receipt
        sealing, and response publication in that order. It never extends the
        owner's fixed budget: both its teardown and receipt polling stop before
        the existing route fallback reserve. Request-scoped route snapshots may
        still race this worker, but the worker marker prevents them from proving
        cleanup until teardown has retired and committed its monotonic receipt.
        """

        _require(
            "deadline_budget_ms" in payload,
            "invalid_infer_cancel_control",
        )
        started_at = time.monotonic()
        result = self._infer_cancel(payload, defer_cleanup=False)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            return result

        # The route has already removed its own fallback-snapshot reserve from
        # the duration carried in ``deadline_budget_ms``.  Teardown deliberately
        # stops one further reserve before this deadline inside
        # ``_apply_cancellation_until_deadline``.  Keep that remaining interval
        # for lifecycle-owned receipt sealing instead of subtracting the same
        # reserve a second time here.  The transmitted budget (and therefore
        # the original route owner deadline) is unchanged.
        inline_deadline = started_at + max(
            0.0,
            payload["deadline_budget_ms"] / 1_000.0,
        )
        self._seal_cancellation_receipt_until_deadline(
            payload,
            deadline_monotonic_s=inline_deadline,
        )

        def stored_receipt() -> Mapping[str, Any] | None:
            lock = getattr(self, "_control_lock", None)
            receipts = getattr(self, "_request_cleanup_receipts", None)
            if lock is None or not isinstance(receipts, Mapping):
                return None
            with lock:
                receipt = receipts.get(request_id)
                return dict(receipt) if isinstance(receipt, Mapping) else None

        receipt = stored_receipt()
        if not isinstance(receipt, Mapping) or receipt.get("complete") is not True:
            return result
        details = result.get("details")
        if not isinstance(details, Mapping):
            details = {}
        return self._signed_result(
            "inference_cancelled",
            {
                **details,
                "cleanup_pending": False,
                "request_cleanup": dict(receipt),
            },
        )

    def _rotate(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _exact_fields(payload, {"peer"}, "invalid_rotate_fields")
        peer_data = _exact_fields(
            data["peer"],
            {"node_id", "endpoint_id", "endpoint_addr", "generation"},
            "invalid_peer_fields",
        )
        _require(
            self.state == "RUNNING" and self.transport is not None,
            "invalid_state_for_rotate",
        )
        try:
            peer = PeerBinding(**peer_data)
            self.transport.rotate_peer(peer)
        except (TypeError, ValueError) as exc:
            raise NodeCommandError("invalid_peer_binding") from exc
        self.peer_generation = peer.generation
        return self._signed_result("peer_rotated", {"peer": _plain_json(peer)})

    def close(self) -> None:
        if self.state == "STOPPED":
            return
        if self.transport is not None:
            try:
                self.transport.close()
            except BaseException:
                pass
            self.transport = None
        if self.runtime is not None:
            try:
                self.runtime.close(reason="worker_shutdown")
            except BaseException:
                pass
        if self.sidecar is not None:
            self.sidecar.close()
            self.sidecar = None
        self.router = None
        self.state = "STOPPED"

    def _stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        _exact_fields(payload, set(), "invalid_stop_fields")
        signed: dict[str, Any] | None = None
        if self.signer is not None:
            self.state = "STOPPING"
            signed = self._signed_result("stopping")
        self.close()
        self.stop_requested = True
        return {"state": self.state, "final_observation": signed}

    def dispatch(self, command: Any) -> dict[str, Any]:
        document = _exact_fields(command, _COMMAND_FIELDS, "invalid_command_fields")
        _require(
            document["protocol"] == NODE_CONTROL_PROTOCOL, "invalid_command_protocol"
        )
        _require(
            isinstance(document["command_id"], str) and bool(document["command_id"]),
            "invalid_command_id",
        )
        _require(document["run_id"] == self.run_id, "run_id_mismatch")
        _require(
            document["deployment_id"] == self.deployment_id, "deployment_id_mismatch"
        )
        _require(isinstance(document["command"], str), "invalid_command")
        _require(isinstance(document["payload"], dict), "invalid_command_payload")
        handlers = {
            "hello": self._hello,
            "configure": self._configure,
            "start": self._start,
            "health": self._health,
            "snapshot": self._snapshot,
            "inbound_admission_snapshot": self._inbound_admission_snapshot,
            "cancel": self._cancel,
            "bind_request_control": self._bind_request_control,
            "update_request_control": self._update_request_control,
            "infer_start": self._infer_start,
            "infer_decode": self._infer_decode,
            "infer_cancel": self._infer_cancel,
            "infer_cancel_wait": self._infer_cancel_wait,
            "rotate": self._rotate,
            "stop": self._stop,
            "a10_target_verify": self._a10_target_verify,
            "a10_batch_policy": self._a10_batch_policy,
            "a7_snapshot": self._a7_snapshot,
            "a7_source_lost": self._a7_source_lost,
            "a11_snapshot": self._a11_snapshot,
            "a11_draft_loss": self._a11_draft_loss,
            "diagnostic_evidence": self._diagnostic_evidence,
        }
        handler = handlers.get(document["command"])
        _require(handler is not None, "unsupported_command")
        return handler(document["payload"])

    def evidence_directory(self) -> Path:
        """``<staging_root>.evidence``: beside the staging root, never inside it."""

        root = Path(self.artifact_root)
        return root.parent / f"{root.name}.evidence"

    def _evidence_document(
        self,
        reason: str,
        *,
        request_id: str | None,
        prefill: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        ring = _STDERR_RING
        stderr_text = "" if ring is None else redact_evidence_text(ring.text())
        transport_state: Any
        reader = getattr(getattr(self, "transport", None), "diagnostic_state", None)
        if callable(reader):
            try:
                transport_state = _plain_json(reader())
            except Exception as exc:  # noqa: BLE001 - evidence never raises
                transport_state = {"unavailable": type(exc).__name__[:64]}
        else:
            transport_state = {"unavailable": "no_diagnostic_state"}
        try:
            threads = _thread_stacks()
        except Exception as exc:  # noqa: BLE001
            threads = [{"name": "unavailable", "daemon": None, "stack": [type(exc).__name__]}]
        return {
            "protocol": _EVIDENCE_PROTOCOL,
            "node_id": self.node_id,
            "reason": reason,
            "request_id": request_id,
            "emitted_at_unix_ms": int(time.time() * 1000),
            "state": str(getattr(self, "state", "unknown"))[:32],
            "stderr": {
                "ring_limit_bytes": None if ring is None else ring.limit_bytes,
                "bytes_seen": None if ring is None else ring.total_bytes,
                "text": stderr_text,
            },
            "threads": threads,
            "transport": transport_state,
            "prefill": None if prefill is None else dict(prefill),
        }

    def write_evidence_bundle(
        self,
        reason: str,
        *,
        request_id: str | None = None,
        prefill: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write one bounded evidence bundle; never raises.

        Returns ``{"file": <name or None>, "document": <bundle>}`` and prints
        one ``MYCELIUM-EVIDENCE-BUNDLE`` pointer line (file name only).
        """

        document: dict[str, Any] = {"protocol": _EVIDENCE_PROTOCOL, "reason": reason}
        try:
            document = self._evidence_document(
                reason, request_id=request_id, prefill=prefill
            )
            encoded = canonical_json_bytes(document)
            if len(encoded) > _EVIDENCE_BUNDLE_MAX_BYTES:
                # Keep the most recent stderr that fits; stacks and state stay.
                text = document["stderr"]["text"]
                overflow = len(encoded) - _EVIDENCE_BUNDLE_MAX_BYTES + 1024
                document["stderr"]["text"] = text.encode("utf-8")[overflow:].decode(
                    "utf-8", errors="ignore"
                )
                encoded = canonical_json_bytes(document)
            if len(encoded) > _EVIDENCE_BUNDLE_MAX_BYTES:
                document["threads"] = document["threads"][:4]
                encoded = canonical_json_bytes(document)
        except Exception:  # noqa: BLE001 - evidence never changes control flow
            return {"file": None, "document": document}
        with _EVIDENCE_SEQUENCE_LOCK:
            sequence = next(_EVIDENCE_SEQUENCE)
        name = (
            f"evidence-{_safe_evidence_token(self.node_id)}-"
            f"{int(time.time() * 1000)}-{sequence:04d}-"
            f"{_safe_evidence_token(reason)}.json"
        )
        try:
            directory = self.evidence_directory()
            directory.mkdir(mode=0o700, exist_ok=True)
            metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise OSError("evidence directory unsafe")
            os.chmod(directory, 0o700)
            temporary = directory / f".{name}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            os.replace(temporary, directory / name)
            bundles = sorted(
                directory.glob("evidence-*.json"),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
            )
            for stale in bundles[:-_EVIDENCE_BUNDLES_KEPT]:
                stale.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            return {"file": None, "document": document}
        _emit_stderr_line(
            f"MYCELIUM-EVIDENCE-BUNDLE reason={_safe_evidence_token(reason)} file={name}"
        )
        return {"file": name, "document": document}

    def _diagnostic_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write and return this node's evidence bundle (route diagnostics).

        Read-only towards the request and transport state; runs on the
        control lane so a wedged inference command cannot block it.
        """

        _exact_fields(payload, {"reason"}, "invalid_diagnostic_evidence_fields")
        reason = payload["reason"]
        _require(
            isinstance(reason, str) and _PREFILL_DIAGNOSTIC_CODE.fullmatch(reason) is not None,
            "invalid_diagnostic_evidence_reason",
        )
        written = self.write_evidence_bundle(reason)
        return {**written["document"], "file": written["file"]}

    def _a10_target_verify(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one A10 target-verification operation on this node's own runtime.

        The record comes from the loaded runtime's verifier only; a refusal is a
        named code and never carries exception text.
        """

        from mycelium_router.a10_product_verifier import A10ProductVerifierError

        _require(self.state == "RUNNING", "invalid_state_for_a10_target_verify")
        try:
            result = _a10_execute_product_operation(self.runtime, payload)
        except A10ProductVerifierError as exc:
            raise NodeCommandError(exc.code) from None
        except Exception as exc:  # noqa: BLE001 - only a stable code leaves the node
            code = getattr(exc, "code", None)
            if isinstance(code, str) and _PREFILL_DIAGNOSTIC_CODE.fullmatch(code):
                raise NodeCommandError(f"a10_target_verify_failed:{code}") from None
            raise NodeCommandError("a10_target_verify_failed") from None
        return _plain_json(result)

    def _a10_batch_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Switch this node's runtime batch policy for the A10 paired benchmark.

        ``baseline`` caps runtime batches at one member (target-only execution);
        ``candidate`` restores the configured cap, and ``candidate@<ms>`` also sets
        the live hop collection window.  Only the relay's batch cap and window
        change: pipeline overlap, the runtime and the route stay as they are.
        The receipt echoes the wire policy with the cap and window now in force;
        the A10 publication shows the effect, since batched intervals carry a
        ``runtime_batch_ref`` only while the candidate cap admits a batch.
        """

        _exact_fields(payload, {"policy"}, "invalid_a10_batch_policy_fields")
        policy = payload["policy"]
        mode, window_ms = parse_a10_batch_policy(policy)
        with self._control_lock:
            _require(
                self.state == "RUNNING" and self.router is not None,
                "invalid_state_for_a10_batch_policy",
            )
            if mode == "baseline":
                config = replace(self._router_config, maximum_runtime_batch_size=1)
            elif window_ms is None:
                config = self._router_config
            else:
                config = replace(
                    self._router_config,
                    live_hop_collection_window_seconds=window_ms / 1_000.0,
                )
            relay = self.router.relay
            relay.batch_scheduler.config = config
            relay.batch_controller.config = config
        return {
            "protocol": A10_BATCH_POLICY_RECEIPT_PROTOCOL,
            "policy": policy,
            "maximum_runtime_batch_size": config.maximum_runtime_batch_size,
            "collection_window_ms": round(
                config.live_hop_collection_window_seconds * 1_000.0, 3
            ),
        }

    def _hello(self, payload: dict[str, Any]) -> dict[str, Any]:
        _exact_fields(payload, set(), "invalid_hello_fields")
        # Discovery needs both run-scoped identities; do not broaden the signed
        # event/snapshot identity schemas or expose the underlying host material.
        return {**self._identity(), "protocol": NODE_CONTROL_PROTOCOL,
                "boot_id": self.boot_id}


def _win32_private_root(path: Path) -> bool:
    """A capacity root must be an absolute owner-only directory (win32 DACL)."""

    if not path.is_absolute():
        return False
    try:
        with mycelium_host_platform.private_directory(path, create=False) as root:
            return bool(root.exists)
    except mycelium_host_platform.HostPlatformError:
        return False


class _Win32DeadlineState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = True
        self.fired = False
        self.delivered = False


_WIN32_DEADLINE_DRAIN_SECONDS = 1.0


@contextmanager
def _win32_command_deadline(seconds: float):
    """Real command deadline where ``setitimer`` does not exist.

    A timer thread marks the deadline as fired and interrupts the main thread
    with a simulated SIGINT.  The SIGINT handler installed for the block turns
    exactly that interrupt into ``NodeCommandError("command_timeout")``; any
    other SIGINT (a real Ctrl+C) still reaches the previous handler.  A
    deadline that fires as the block is leaving is drained before the handler
    is restored, so it can never leak into unrelated code.  C-level work that
    does not return to the interpreter is bounded by the parent's response
    timeout and job kill instead.
    """

    import _thread

    if threading.current_thread() is not threading.main_thread():
        raise NodeCommandError("command_deadline_unavailable")
    state = _Win32DeadlineState()

    def handler(signum: int, frame: Any) -> None:
        if state.fired and not state.delivered:
            state.delivered = True
            raise NodeCommandError("command_timeout")
        if callable(previous):
            previous(signum, frame)
            return
        if previous == signal.SIG_IGN:
            return
        raise KeyboardInterrupt

    def fire() -> None:
        with state.lock:
            if not state.active:
                return
            state.fired = True
            _thread.interrupt_main(signal.SIGINT)

    previous = signal.signal(signal.SIGINT, handler)
    try:
        timer = threading.Timer(seconds, fire)
        timer.daemon = True
        timer.start()
        try:
            yield
        finally:
            with state.lock:
                state.active = False
            timer.cancel()
            if state.fired and not state.delivered:
                drain_until = time.monotonic() + _WIN32_DEADLINE_DRAIN_SECONDS
                while not state.delivered and time.monotonic() < drain_until:
                    time.sleep(0.001)
    finally:
        signal.signal(signal.SIGINT, previous)


@contextmanager
def _command_deadline(seconds: float):
    if mycelium_host_platform.is_win32():
        with _win32_command_deadline(seconds):
            yield
        return
    if not hasattr(signal, "setitimer"):
        # No deadline primitive on this host: refuse rather than run unbounded.
        raise NodeCommandError("command_deadline_unavailable")

    def timeout_handler(_signum: int, _frame: Any) -> None:
        raise NodeCommandError("command_timeout")

    previous = signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def _response(
    service: PhysicalNodeService,
    *,
    command_id: str,
    ok: bool,
    result: Any = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol": NODE_CONTROL_PROTOCOL,
        "command_id": command_id,
        "node_id": service.node_id,
        "ok": ok,
        "route_ready": False,
    }
    if ok:
        response["result"] = _plain_json(result)
    else:
        response["error"] = {"code": error_code or "node_command_failed"}
    return response


def _emit(encoded: bytes, *, cleanup_priority: bool = False) -> None:
    if cleanup_priority:
        # Deadline-bound cleanup authority uses SSH's separately supervised
        # stderr data stream. An ordinary response can already occupy stdout
        # before infer_cancel_wait reaches stdin; no in-process priority queue
        # can reorder bytes after that point. The parent recognizes only this
        # exact prefix and then applies the same canonical response,
        # command-ID, node-ID, and signed-observation validation as stdout.
        # Diagnostics remain unprefixed stderr data.
        sys.stderr.buffer.write(CLEANUP_CONTROL_FRAME_PREFIX + encoded + b"\n")
        sys.stderr.buffer.flush()
        return
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


class _ResponseEmitter:
    """Publish ordinary and cleanup responses on independent wire lanes."""

    def __init__(self) -> None:
        self._stdout_condition = threading.Condition()
        self._stdout_active = False
        self._cleanup_lock = threading.Lock()

    def emit(
        self,
        document: dict[str, Any],
        *,
        cleanup_priority: bool = False,
    ) -> None:
        encoded = canonical_json_bytes(document)
        if cleanup_priority:
            # Cleanup authority has a separately supervised stderr channel.
            # Serialize that channel against itself, but never wait behind a
            # large or backpressured stdout inference response: doing so
            # defeats the independent lane and can strand an already-complete
            # signed receipt beyond its immutable owner deadline.
            with self._cleanup_lock:
                _emit(encoded, cleanup_priority=True)
            return
        with self._stdout_condition:
            # stdout and the deadline-bound cleanup channel are deliberately
            # independent. Reserving stdout for a cleanup response that will
            # never use it couples unrelated request lifecycles: sustained
            # cancellation can then retain at least one reservation forever,
            # starving completed inference responses and creating a backlog of
            # late replies after their parent waiters have already retired.
            while self._stdout_active:
                self._stdout_condition.wait()
            self._stdout_active = True
        try:
            _emit(encoded, cleanup_priority=False)
        finally:
            with self._stdout_condition:
                self._stdout_active = False
                self._stdout_condition.notify_all()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--socket-root", type=Path, required=True)
    parser.add_argument("--capacity-root", type=Path)
    parser.add_argument("--sidecar-binary", type=Path, required=True)
    network_mode = parser.add_mutually_exclusive_group()
    network_mode.add_argument("--sidecar-local-only", action="store_true")
    network_mode.add_argument("--sidecar-force-relay", action="store_true")
    network_mode.add_argument("--sidecar-lan", action="store_true")
    parser.add_argument("--endpoint-secret-file", type=Path)
    parser.add_argument("--command-timeout", type=float, default=30.0)
    parser.add_argument(
        "--decode-mode",
        choices=("complete_context_replay", "stage_local_kv"),
    )
    return parser.parse_args()


def main() -> int:
    # The node's own stderr ring for evidence bundles (A14 class D, round 2).
    _install_stderr_ring()
    args = _parse_args()
    try:
        service = PhysicalNodeService(
            run_id=args.run_id,
            deployment_id=args.deployment_id,
            node_id=args.node_id,
            artifact_root=args.artifact_root,
            socket_root=args.socket_root,
            capacity_root=args.capacity_root,
            sidecar_binary=args.sidecar_binary,
            sidecar_local_only=args.sidecar_local_only,
            sidecar_force_relay=args.sidecar_force_relay,
            sidecar_lan=getattr(args, "sidecar_lan", False),
            command_timeout=args.command_timeout,
            endpoint_secret_file=args.endpoint_secret_file,
            requested_decode_mode=args.decode_mode,
        )
    except (NodeCommandError, OSError) as exc:
        print(f"physical-node startup rejected: {type(exc).__name__}", file=sys.stderr)
        return 2

    # CPython runs this handler on the main thread between bytecodes.  It
    # must take no lock: closing the service here (transport, runtime and
    # sidecar locks) can wait on a lock the interrupted main thread holds.
    # It only records the stop and unwinds; the main thread closes the
    # service first thing in the ``finally`` below.  A repeated signal does
    # not interrupt that cleanup.
    stop_signalled = mycelium_host_platform.StopRequest()

    def stop_on_signal(signum: int, _frame: Any) -> None:
        if stop_signalled.is_set():
            return
        stop_signalled.set()
        raise SystemExit(128 + signum)

    prior_signal_handlers = {
        signum: signal.signal(signum, stop_on_signal)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    stop_watch = None
    if mycelium_host_platform.is_win32():
        # Windows delivers no SIGTERM between processes: a supervisor asks for
        # a graceful stop through the named event in MYCELIUM_STOP_EVENT, which
        # is routed into the same stop_on_signal path.
        import _thread

        try:
            stop_watch = mycelium_host_platform.start_stop_event_watcher(
                lambda: _thread.interrupt_main(signal.SIGTERM)
            )
        except mycelium_host_platform.HostPlatformError:
            for signum, handler in prior_signal_handlers.items():
                signal.signal(signum, handler)
            service.close()
            print("physical-node startup rejected: stop_event", file=sys.stderr)
            return 2

    response_emitter = _ResponseEmitter()
    active_lock = threading.Lock()
    active_command_ids: set[str] = set()
    inference_capacity = threading.BoundedSemaphore(8)
    control_capacity = threading.BoundedSemaphore(16)
    cleanup_capacity = threading.BoundedSemaphore(16)

    def execute(
        command: Any,
        command_id: str,
        lane_capacity: threading.BoundedSemaphore,
        received_at_monotonic_s: float,
    ) -> None:
        cleanup_priority = _command_uses_cleanup_response_priority(command)
        try:
            # Long inference commands own absolute deadlines and cooperative
            # cancellation points. The stdin reader must stay free to accept
            # cancellation, cleanup, probes, and unrelated request commands.
            result = service.dispatch(
                _age_cleanup_command_budget(
                    command,
                    queued_seconds=max(
                        0.0,
                        time.monotonic() - received_at_monotonic_s,
                    ),
                )
            )
            response_emitter.emit(
                _response(service, command_id=command_id, ok=True, result=result),
                cleanup_priority=cleanup_priority,
            )
            service.run_after_response()
        except NodeCommandError as exc:
            response_emitter.emit(
                _response(
                    service, command_id=command_id, ok=False, error_code=exc.code
                ),
                cleanup_priority=cleanup_priority,
            )
        except IrohTransportError as exc:
            if _owner_cancellation_interrupted_inference(service, command, exc):
                response_emitter.emit(
                    _response(
                        service,
                        command_id=command_id,
                        ok=False,
                        error_code="request_cancelled",
                    ),
                    cleanup_priority=cleanup_priority,
                )
            else:
                print(
                    f"physical-node command failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                response_emitter.emit(
                    _response(
                        service,
                        command_id=command_id,
                        ok=False,
                        error_code="node_command_failed",
                    ),
                    cleanup_priority=cleanup_priority,
                )
        except BaseException as exc:
            print(
                f"physical-node command failed: {type(exc).__name__}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            response_emitter.emit(
                _response(
                    service,
                    command_id=command_id,
                    ok=False,
                    error_code="node_command_failed",
                ),
                cleanup_priority=cleanup_priority,
            )
        finally:
            service.discard_after_response()
            with active_lock:
                active_command_ids.discard(command_id)
            lane_capacity.release()

    # Long inference commands and cleanup/liveness control use independently
    # bounded lanes.  Retired or slow model execution can consume only the
    # inference lane; generation-fenced cancellation, cleanup snapshots, and
    # health probes retain reserved workers and cannot queue behind it.
    inference_executor = ThreadPoolExecutor(
        max_workers=8,
        thread_name_prefix="mycelium-node-inference",
    )
    control_executor = ThreadPoolExecutor(
        max_workers=16,
        thread_name_prefix="mycelium-node-control",
    )
    cleanup_executor = ThreadPoolExecutor(
        max_workers=16,
        thread_name_prefix="mycelium-node-cleanup",
    )
    try:
        for raw_line in sys.stdin.buffer:
            received_at_monotonic_s = time.monotonic()
            command_id = "unknown"
            command: Any = None
            if len(raw_line) > MAX_COMMAND_BYTES:
                response_emitter.emit(
                    _response(
                        service,
                        command_id=command_id,
                        ok=False,
                        error_code="command_too_large",
                    )
                )
                continue
            try:
                command = canonical_json_loads(raw_line.rstrip(b"\n"), path="stdin")
                if isinstance(command, dict) and isinstance(
                    command.get("command_id"), str
                ):
                    command_id = command["command_id"]
                inference_lane = _command_uses_inference_lane(command)
                cleanup_lane = _command_uses_cleanup_response_priority(command)
                if cleanup_lane:
                    lane_capacity = cleanup_capacity
                    lane_executor = cleanup_executor
                elif inference_lane:
                    lane_capacity = inference_capacity
                    lane_executor = inference_executor
                else:
                    lane_capacity = control_capacity
                    lane_executor = control_executor
                if not lane_capacity.acquire(blocking=False):
                    response_emitter.emit(
                        _response(
                            service,
                            command_id=command_id,
                            ok=False,
                            error_code="command_capacity_exhausted",
                        ),
                        cleanup_priority=(
                            _command_uses_cleanup_response_priority(command)
                        ),
                    )
                    continue
                with active_lock:
                    if command_id in active_command_ids:
                        lane_capacity.release()
                        response_emitter.emit(
                            _response(
                                service,
                                command_id=command_id,
                                ok=False,
                                error_code="duplicate_command_id",
                            ),
                            cleanup_priority=(
                                _command_uses_cleanup_response_priority(command)
                            ),
                        )
                        continue
                    active_command_ids.add(command_id)
                lane_executor.submit(
                    execute,
                    command,
                    command_id,
                    lane_capacity,
                    received_at_monotonic_s,
                )
                if isinstance(command, dict) and command.get("command") == "stop":
                    break
            except NodeCommandError as exc:
                response_emitter.emit(
                    _response(
                        service, command_id=command_id, ok=False, error_code=exc.code
                    ),
                    cleanup_priority=_command_uses_cleanup_response_priority(command),
                )
            except BaseException as exc:
                print(
                    f"physical-node command failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
                response_emitter.emit(
                    _response(
                        service,
                        command_id=command_id,
                        ok=False,
                        error_code="node_command_failed",
                    ),
                    cleanup_priority=_command_uses_cleanup_response_priority(command),
                )
        return 0
    finally:
        if stop_signalled.is_set():
            # Abort in-flight commands before draining the executors; a close
            # failure here is reported by the final close below.
            try:
                service.close()
            except Exception:
                pass
        inference_executor.shutdown(wait=True, cancel_futures=False)
        control_executor.shutdown(wait=True, cancel_futures=False)
        cleanup_executor.shutdown(wait=True, cancel_futures=False)
        if stop_watch is not None:
            stop_watch.set()
        for signum, handler in prior_signal_handlers.items():
            signal.signal(signum, handler)
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
