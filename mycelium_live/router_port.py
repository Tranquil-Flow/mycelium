# SPDX-License-Identifier: AGPL-3.0-only
"""Adapt the request gateway's RouterPort onto a persistent LiveRoute."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, cast, Iterable, Mapping

import mycelium_deadlines
from mycelium_a6_m16 import M16RecoveryAuthority
from mycelium_a6_recovery import ReplayCheckpoint
from mycelium_m16_runtime import M16AdmissionError, M16RuntimeCoordinator
from mycelium_router.contracts import ExecutionGraph, PathManifest, RequestContext
from mycelium_router.serialization import path_manifest_to_dict

from .command_controller import (
    CleanupResult,
    CleanupStatus,
    CommandController,
    CommandEnvelope,
    CommandIdentity,
    CommandKind,
    TerminalResult,
    TerminalStatus,
)
from .a4_contracts import validate_interruptible_stage_command
from .lock_order import LockOrderDetector, LockOrderViolation

from .route import AffectedPeerQuarantined, InferenceCancelled, LiveRoute, TokenSink

# The owner's cancellation-and-cleanup window, bound from the one registry record
# the rest of the composition partitions -- never a second route-local copy.  A
# 2.0 s literal here mints deadlines the node's own advertised bound cannot honour
# (D9); BOUND_CONSUMER_BINDINGS keeps this from drifting.
_MAX_CANCELLATION_AND_CLEANUP_SECONDS = mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS


_LOGGER = logging.getLogger(__name__)
def _source_failure_matches(
    identity: Mapping[str, Any], binding: Mapping[str, Any]
) -> bool:
    """Exact source identity, except the failure's own cancellation generation.

    A physical source failure interrupts the command, which joins the route's
    next cancellation generation before the router records the failure. A
    binding captured while the source was executing therefore carries the
    generation the failure started from: it may trail the observed one, never
    lead it. Every other identity field must match exactly.
    """

    for field, observed in identity.items():
        bound = binding.get(field)
        if field == "cancellation_generation":
            if (
                type(bound) is not int
                or type(observed) is not int
                or not 0 <= bound <= observed
            ):
                return False
        elif bound != observed:
            return False
    return True


#: Released requests whose observed source-failure identity stays readable.
#: The gateway releases a request as soon as its backend run returns, which
#: for a synchronously refused recovery happens before a controlled-fault
#: observer can poll; the bounded map keeps that one transition observable.
_RETAINED_SOURCE_FAILURES_MAX = 256
# Finished requests carried in the public recovery projection across a
# qualification binding change (cold route replacement or idle renewal), so a
# refresh still reconstructs a recovery that ended just before the change.
# Bounded by count (oldest finished first) and by age since the change
# (_ROTATED_TERMINAL_RECORD_MAX_AGE_MS, below the recovery deadline bound).
_ROTATED_TERMINAL_RECORDS_MAX = _RETAINED_SOURCE_FAILURES_MAX

# A withheld terminal is the route's most consequential refusal: the client sees a stream that simply ends.
# Until round p78r1 (ERRATA-23) the reason lived only in ``LiveRoute._incidents`` -- in memory -- so it died
# with the app and the round had to be reconstructed from four artifacts over three attempts, still unable to
# split "the cleanup-proof command was issued and went unclaimed" from "no proof command was ever minted".
# This record makes that split one file read: a bounded, privacy-reduced line per withheld terminal, whose
# command-id prefix can be joined against the owner's managed-control ledger.
#
# It carries the CAUSE as well as the refusal: ``route_error_code`` is the route's own bounded failure code
# (``_bounded_error_code``, computed one line above the block site) and ``scoped_incident`` names which
# recovery branch ran.  Round p78r1 is why -- 103 blocks, all with the identical reason
# ``command_cleanup_receipt_missing``, while the failure underneath was discarded at the block site, so the
# round could not say what the route had hit.  A diagnostic that keeps only the refusal is the
# "records where, never why" class this program has now paid for four times.
#
# Privacy: id PREFIXES only (16 chars, the convention the node pump's diagnostics already use), no payload,
# no claim token, no key material, no request text.  Bounded: 128 KiB then ONE rotation, so a soak cannot
# grow it without limit.  Never raises: a diagnostic may not perturb the request path.
#
# WHERE IT LANDS, and why the env comes first: the route classes carry no state root (measured -- zero
# attributes by any of the names searched below), and the two production construction sites have no root in
# scope either, so there is no honest default.  The OPERATOR sets MYCELIUM_LIVE_BLOCKED_TERMINAL_LOG to a
# path inside the round's evidence directory (the e-chain does this); the root-attribute fallback below only
# serves a host that happens to expose one.  Consequence worth stating where a reader will see it: if a round
# withholds terminals and this file is ABSENT, that is a finding about the instrument, not a silence about
# the route -- record it as such rather than concluding no terminal was blocked.
_BLOCKED_TERMINAL_PROTOCOL = "mycelium.live_route_blocked_terminal.v1"
_BLOCKED_TERMINAL_LOG_ENV = "MYCELIUM_LIVE_BLOCKED_TERMINAL_LOG"
_BLOCKED_TERMINAL_LOG_NAME = "route-blocked-terminals.jsonl"
_BLOCKED_TERMINAL_LOG_MAX_BYTES = 131_072
_ID_PREFIX_CHARS = 16


def _blocked_terminal_log_path(route: Any) -> Path | None:
    """Where the withheld-terminal record goes, or None when the route exposes no writable root.

    Truthiness is checked rather than type alone: a MagicMock or a test double answers every ``getattr`` with
    something, and ``Path(<Mock>)`` would raise inside a diagnostic path that must stay silent.
    """

    override = os.environ.get(_BLOCKED_TERMINAL_LOG_ENV)
    if override:
        return Path(override)
    for attribute in ("evidence_root", "state_root", "_root", "_evidence_root"):
        root = getattr(route, attribute, None)
        if isinstance(root, (str, os.PathLike)) and str(root):
            return Path(root) / _BLOCKED_TERMINAL_LOG_NAME
    return None


def _record_blocked_terminal(
    *,
    route: Any,
    request_id: str,
    reason: str,
    pending: Any,
    route_error_code: str | None = None,
    scoped_incident: bool = False,
) -> None:
    """Persist which terminal was withheld, why, and which command it could not prove -- bounded and silent.

    The command-id prefix is the join key: matched against ``managed_commands`` it tells the next round
    whether that command was ever minted, what operation it carried, its declared TTL and its terminal state.

    ``route_error_code`` is the route's OWN bounded failure code -- the cause, as opposed to ``reason``,
    which is only the port's sentence for withholding.  Both are needed: in round p78r1 all 103 blocks
    carried the single reason ``command_cleanup_receipt_missing`` while the failure underneath (already
    computed one line above the block site) was discarded, so the round could not say what the route had
    actually hit.  ``scoped_incident`` records which of the two following branches ran, because they differ
    in capacity: ``complete()`` retires the runtime reservation, ``retire_dispatch_slot()`` retains it.
    """

    try:
        path = _blocked_terminal_log_path(route)
        if path is None:
            return
        identity = getattr(pending, "command_identity", None)

        def _field(name: str) -> Any:
            # ``command_identity`` is a CommandIdentity dataclass in the product and a Mapping in some
            # doubles; both shapes must be readable or the join key silently comes out empty (measured once:
            # a Mapping-only read wrote command_id_prefix "" and the record could not be joined).
            if isinstance(identity, Mapping):
                return identity.get(name)
            return getattr(identity, name, None)

        record = {
            "protocol": _BLOCKED_TERMINAL_PROTOCOL,
            "observed_at_unix_ms": int(time.time() * 1_000),
            "request_id_prefix": str(request_id)[:_ID_PREFIX_CHARS],
            "reason": str(reason)[:128],
            "route_error_code": (str(route_error_code)[:64] if route_error_code else None),
            "scoped_incident": bool(scoped_incident),
            "command_id_prefix": str(_field("command_id") or "")[:_ID_PREFIX_CHARS],
            "request_attempt": _field("request_attempt"),
            "path_id_prefix": str(_field("path_id") or "")[:_ID_PREFIX_CHARS],
            "path_attempt": _field("path_attempt"),
            "absolute_deadline_ms": _field("absolute_deadline_ms"),
            "execution_started": bool(getattr(pending, "execution_started", False)),
            "cleanup_owner_bound": getattr(pending, "cleanup_owner_id", None) is not None,
            "cleanup_receipt_present": getattr(pending, "cleanup_receipt", None) is not None,
        }
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size + len(line) > _BLOCKED_TERMINAL_LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        # Deliberately silent: a read-only home, a full disk, or a route double with no root must never turn
        # a withheld terminal into a different failure.  The record is a diagnostic, not a gate.
        pass


#: Expiry of the frozen recovery deadline as reported by the checkpoint
#: authority and the successor qualifier.
_RECOVERY_EXPIRY_CODES = frozenset({
    "recovery_qualification_expired",
    "recovery_checkpoint_stale",
    "recovery_deadline_exceeded",
})
#: Longest single wait of the recovery deadline guard; the injected recovery
#: clock is re-read after every wait.
_RECOVERY_DEADLINE_POLL_SECONDS = 0.25
#: Upper bound for which an armed A6 fault hold may delay a successor's
#: completion cleanup. The hold exists only once the fault control arms it.
_A6_COMPLETION_CLEANUP_HOLD_SECONDS = 30.0
# Shared with the embedded owner stop allowance; no deadline is extended.
ROUTER_CLOSE_TIMEOUT_SECONDS = 4.0


#: Largest accepted recovery deadline: the successor command inherits the
#: source command's absolute deadline, which is one hour after admission.
_MAX_RECOVERY_DEADLINE_MS = 3_600_000
# A carried terminal record outlives the binding change by at most the longest
# admissible recovery, so an observer that reloads during the bounded window a
# recovery may take still reconstructs how it ended.
_ROTATED_TERMINAL_RECORD_MAX_AGE_MS = _MAX_RECOVERY_DEADLINE_MS
#: Environment overrides (seconds) for the frozen per-phase recovery deadlines.
_RECOVERY_DEADLINE_ENVIRONMENT = (
    ("qualification_ms", "MYCELIUM_A6_RECOVERY_QUALIFICATION_SECONDS"),
    ("prefill_ms", "MYCELIUM_A6_RECOVERY_PREFILL_SECONDS"),
    ("progress_ms", "MYCELIUM_A6_RECOVERY_PROGRESS_SECONDS"),
    ("cutover_ms", "MYCELIUM_A6_RECOVERY_CUTOVER_SECONDS"),
    ("total_ms", "MYCELIUM_A6_RECOVERY_TOTAL_SECONDS"),
)


@dataclass(frozen=True)
class RecoveryPhaseDeadlines:
    """Frozen per-phase bounds of one full-context replay recovery.

    A6 spec section 5 freezes successor qualification, prefill, cutover and
    total recovery deadlines separately. One checkpoint expiry used to bound
    qualification, replay prefill and the whole buffered continuation, so a
    normal-length answer on a slow successor could never recover. Each phase
    now has its own bound and a stalled successor still aborts at the first
    bound it misses:

    * ``qualification_ms``: recovery attempt start to a qualified successor
      reservation (the fleet's physical challenge measured about 6 s).
    * ``prefill_ms``: successor start to its first verified next position.
    * ``progress_ms``: longest gap between successor tokens once they flow.
    * ``cutover_ms``: successor output finished to the cutover CAS (includes
      the fresh qualification re-read before publication).
    * ``total_ms``: hard cap from the checkpoint freeze; it is the replay
      checkpoint's expiry, so neither qualification nor cutover can succeed
      after it.
    """

    qualification_ms: int = 30_000
    prefill_ms: int = 30_000
    progress_ms: int = 15_000
    cutover_ms: int = 30_000
    total_ms: int = 300_000

    def __post_init__(self) -> None:
        values = (
            self.qualification_ms,
            self.prefill_ms,
            self.progress_ms,
            self.cutover_ms,
            self.total_ms,
        )
        if any(
            type(value) is not int or not 0 < value <= _MAX_RECOVERY_DEADLINE_MS
            for value in values
        ) or any(value > self.total_ms for value in values[:-1]):
            raise ValueError("a6_recovery_deadlines_invalid")

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None,
    ) -> "RecoveryPhaseDeadlines":
        """Defaults, overridden by ``MYCELIUM_A6_RECOVERY_*_SECONDS``."""

        source = os.environ if environ is None else environ
        overrides: dict[str, int] = {}
        for name, variable in _RECOVERY_DEADLINE_ENVIRONMENT:
            raw = source.get(variable)
            if raw is None or not raw.strip():
                continue
            try:
                seconds = float(raw)
            except ValueError:
                raise ValueError("a6_recovery_deadlines_invalid") from None
            if not math.isfinite(seconds):
                raise ValueError("a6_recovery_deadlines_invalid")
            overrides[name] = int(round(seconds * 1_000))
        total_ms = overrides.get("total_ms")
        if total_ms is not None:
            # A lowered total also lowers the phase defaults it would
            # otherwise undercut; explicitly configured phases stay
            # validated against it.
            for name, _variable in _RECOVERY_DEADLINE_ENVIRONMENT[:-1]:
                if name not in overrides:
                    default = cls.__dataclass_fields__[name].default
                    overrides[name] = min(default, total_ms) if total_ms > 0 else default
        return cls(**overrides)


class _RecoveryDeadlineGuard:
    """Interrupt in-flight successor work once its current phase deadline passes.

    ``deadline`` returns the bound that applies now and the phase it belongs
    to (prefill until the first successor token, then progress since the last
    token, capped by the total recovery deadline); it is re-read after every
    wait. Without this guard a stalled successor would hold the request until
    the command's absolute deadline (one hour) instead of failing at that bound.
    """

    def __init__(self, *, deadline: Callable[[], tuple[int, str]],
                 clock_ms: Callable[[], int],
                 interrupt: Callable[[Callable[[], bool], str], bool | None]) -> None:
        self._deadline = deadline
        self._clock_ms = clock_ms
        self._interrupt = interrupt
        self._done = threading.Event()
        self.fired = False
        self._thread = threading.Thread(
            target=self._run, name="a6-recovery-deadline", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._done.is_set():
            deadline_unix_ms, phase = self._deadline()
            remaining_ms = deadline_unix_ms - self._clock_ms()
            if remaining_ms > 0:
                self._done.wait(
                    min(_RECOVERY_DEADLINE_POLL_SECONDS, remaining_ms / 1_000.0))
                continue
            try:
                # ``None``: the bound moved (a token arrived) between the read
                # above and the interruption's own check under the router lock.
                result = self._interrupt(self._done.is_set, phase)
            except Exception:
                # The interruption itself failed; the work still ran past the
                # deadline, so its result can never be published.
                self.fired = True
                return
            if result is None:
                continue
            self.fired = bool(result)
            return

    def stop(self) -> None:
        self._done.set()

    def join(self, timeout: float) -> None:
        """Wait (bounded) until a started interruption has returned.

        The interruption owns the route cancellation fan-out, which publishes
        the successor's exact cleanup receipt. The interrupted worker can
        return before that fan-out does; recording the command terminal in
        that gap finds no receipt (``command_cleanup_receipt_missing``) and
        blocks a recovery that is about to be proven clean.
        """

        if self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)


def _route_that_ran(route: Any, request_id: str) -> Any:
    """The concrete route that executed ``request_id``, else ``route``.

    A cold-restart owner replaces its route when every peer is lost. The
    replacement never ran the request, so its cleanup evidence (exact
    receipts, exited-peer incidents) is read from the retired route that
    did; the owner keeps that mapping until the request is released.
    """

    reader = getattr(type(route), "route_for_request", None)
    if callable(reader):
        try:
            ran = route.route_for_request(request_id)
        except Exception:
            ran = None
        if ran is not None:
            return ran
    return route


@dataclass
class _Pending:
    request: RequestContext | None = None
    placement_ids: tuple[str, ...] | None = None
    route_identity: dict[str, Any] | None = None
    path_manifest: dict[str, Any] | None = None
    command_identity: CommandIdentity | None = None
    #: A11 preference for the first attempt only (``None`` = not stated).
    speculation_preference: str | None = None
    cleanup_owner_id: str | None = None
    cleanup_receipt: dict[str, Any] | None = None
    tokens: list[tuple[int, int]] = field(default_factory=list)
    cursor: int = 0
    terminal_status: str | None = None
    terminal_error_code: str | None = None
    terminal_blocked_reason: str | None = None
    cancellation_linearized: bool = False
    cancellation_requested: threading.Event = field(default_factory=threading.Event)
    release_requested: bool = False
    execution_started: bool = False
    recovery_required: bool = False
    recovery_phase: str = "active"
    source_failure_identity: dict[str, Any] | None = None
    recovery_attempts: int = 0
    recovery_failure_reason: str | None = None
    recovery_excluded_placements: set[str] = field(default_factory=set)
    #: A frozen recovery phase deadline elapsed (successor work in flight was
    #: interrupted, or the recovery stopped before the next phase).
    recovery_deadline_exceeded: bool = False
    #: Which bound fired: ``qualification``, ``prefill``, ``progress``,
    #: ``cutover`` or ``total``; ``None`` while none has.
    recovery_deadline_phase: str | None = None
    #: Recovery-clock time of the latest buffered successor token.
    recovery_last_progress_unix_ms: int | None = None
    #: Successor output buffered before cutover, and the successor's bound;
    #: counts only, never token material.
    recovery_output_count: int = 0
    recovery_output_limit: int | None = None
    #: The route finished the successor's generation and asked to start its
    #: completion cleanup; set only outside an owner cancellation.
    recovery_output_finished: bool = False
    #: A6 fault control only: an armed hold delays the successor's completion
    #: cleanup authorization until released (bounded); ``None`` in production.
    a6_completion_cleanup_hold: threading.Event | None = None
    a6_completion_cleanup_held: bool = False


#: Upper bound of the A10 candidate collection window (``physical_inference_node``
#: refuses anything above the same bound).
A10_MAXIMUM_COLLECTION_WINDOW_MS = 250


class LiveRouterPort:
    """Drive one persistent physical route through the RouterPort contract."""

    requires_qualification_binding = True
    supports_workload_profiles = True
    supports_publisher_generation = True
    #: A11: an explicit future-request speculation preference reaches the
    #: request's first route attempt only; recoveries run target-only.
    supports_speculation_preference = True

    def __init__(
        self,
        *,
        route: LiveRoute,
        execution_graph: ExecutionGraph,
        runtime_coordinator: M16RuntimeCoordinator | None = None,
        lock_order_detector: LockOrderDetector | None = None,
        recovery_authority: M16RecoveryAuthority | None = None,
        recovery_qualification_source: (
            Callable[
                [str, ReplayCheckpoint, Mapping[str, Any], PathManifest],
                tuple[Mapping[str, Any], Mapping[str, Any]],
            ]
            | None
        ) = None,
        recovery_tokenizer_digest: str | None = None,
        recovery_a4_atomic_commit: str | None = None,
        recovery_clock_ms: Callable[[], int] | None = None,
        qualification_source: Any | None = None,
        recovery_deadlines: RecoveryPhaseDeadlines | None = None,
    ) -> None:
        recovery_parts = (
            recovery_authority,
            recovery_qualification_source,
            recovery_tokenizer_digest,
        )
        if any(part is not None for part in recovery_parts) and (
            runtime_coordinator is None
            or any(part is None for part in recovery_parts)
        ):
            raise ValueError("a6_recovery_configuration_incomplete")
        if recovery_deadlines is None:
            recovery_deadlines = RecoveryPhaseDeadlines.from_environment()
        if not isinstance(recovery_deadlines, RecoveryPhaseDeadlines):
            raise ValueError("a6_recovery_deadlines_invalid")
        self._recovery_deadlines = recovery_deadlines
        self._route = route
        self._graph = execution_graph
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        # Coordinator admission makes a request dispatch-visible before this
        # adapter has finished binding its command identity and pending state.
        # Serialize that publication boundary with dispatcher selection so a
        # fast dispatcher cannot consume and fail a request in the sub-ms gap.
        self._admission_dispatch_lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._sinks: dict[str, object] = {}
        self._recovery_failures: dict[str, str] = {}
        self._retained_source_failures: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._retained_recovery_attempts: OrderedDict[str, int] = OrderedDict()
        self._retained_terminal_statuses: OrderedDict[str, str] = OrderedDict()
        self._retained_recovery_outcomes: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._coordinator = runtime_coordinator
        self._recovery_authority = recovery_authority
        self._recovery_qualification_source = recovery_qualification_source
        self._recovery_tokenizer_digest = recovery_tokenizer_digest
        self._recovery_a4_atomic_commit = recovery_a4_atomic_commit
        self._recovery_qualification_digest: str | None = None
        # The public recovery document has one current binding. Live recovery
        # state is listed only for request IDs admitted under that binding;
        # a request that finished just before the binding changed is carried
        # below (bounded) with its terminal facts only, so a refresh still
        # reconstructs it (A6 spec section 7). Older ones remain in
        # coordinator history only.
        self._recovery_current_request_ids: set[str] = set()
        # Released terminal requests admitted under an earlier binding of this
        # same deployment and graph, carried when the binding changed (request
        # ID -> carry time in recovery-clock ms). Bounded by
        # _ROTATED_TERMINAL_RECORDS_MAX and _ROTATED_TERMINAL_RECORD_MAX_AGE_MS;
        # listed only while the coordinator reports them terminal.
        self._recovery_rotated_terminal_ids: OrderedDict[str, int] = OrderedDict()
        # Qualifier-owned source of the *current* deployment qualification,
        # shared with the request gateway's qualification gate. Admission
        # binds the digest on first use; an idle projection (the browser polls
        # before the first request) reads this source instead of failing or
        # inventing a digest.
        self._qualification_source = qualification_source
        self._recovery_clock_ms = recovery_clock_ms or (
            lambda: int(time.time() * 1_000)
        )
        self._commands = CommandController()
        self._lock_order = lock_order_detector or LockOrderDetector()
        self._closed = False
        worker_count = (
            4
            if runtime_coordinator is None
            else runtime_coordinator.maximum_concurrent_requests
        )
        self._worker_pool = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix=f"live-route-worker-{execution_graph.deployment_id[:12]}",
        )
        self._dispatcher = None
        if runtime_coordinator is not None:
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name=f"m16-dispatch-{execution_graph.deployment_id[:16]}",
                daemon=True,
            )
            self._dispatcher.start()

    def current_deployment(self) -> ExecutionGraph:
        return self._graph

    def is_idle(self) -> bool:
        """Return true only after every admitted request has been released."""

        with self._lock:
            return not self._pending

    def run_qualification_refresh_when_idle(
        self, refresh: Callable[[], Any | None]
    ) -> None:
        """Publish a genuine renewed binding in the same admission epoch.

        The callback is the owner's qualified physical challenge. Its result
        is not accepted as authority until the owner has published it; no
        pending request may cross this rotation boundary.
        """
        with self._admission_dispatch_lock:
            with self._lock:
                if self._closed or self._pending:
                    return
            renewed = refresh()
            if renewed is None:
                return
            from mycelium_qualification.contracts import RouteQualificationV1
            from mycelium_request_gateway.contracts import qualification_digest

            if (
                not isinstance(renewed, RouteQualificationV1)
                or renewed.route_ready is not True
                or renewed.deployment_id != self._graph.deployment_id
                or renewed.deployment_epoch != self._graph.deployment_epoch
                or renewed.topology_version != self._graph.topology_version
                or renewed.model_id != self._graph.model_id
                or renewed.resolved_commit != self._graph.resolved_commit
                or renewed.manifest_digest != self._graph.manifest_digest
            ):
                raise M16AdmissionError("qualification_binding_changed")
            digest = qualification_digest(renewed)
            with self._lock:
                if self._closed or self._pending:
                    raise M16AdmissionError("qualification_binding_changed")
                if self._recovery_qualification_digest != digest:
                    self._carry_terminal_requests_across_binding_locked()
                self._recovery_qualification_digest = digest

    def a10_target_verify(
        self,
        node_id: str,
        payload: Mapping[str, Any],
        *,
        timeout_s: float,
    ) -> tuple[str, dict[str, Any]]:
        """Dispatch one A10 target-verification operation, admitted exclusively.

        The operation is validated, then admitted only on an open, alive and
        idle route while the admission lock is held, so no request can be
        admitted while the target runtime verifies.  It is sent through the
        route to a node of the current deployment, and only the record for
        exactly this operation is accepted.
        """

        from mycelium_router.a10_product_verifier import (
            unwrap_product_result,
            validate_operation_payload,
        )

        if not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 120:
            raise ValueError("a10_verify_deadline_invalid")
        operation = validate_operation_payload(payload)
        deployment_nodes = {
            placement.node_id
            for stage in self._graph.stages
            for placement in stage.placements
        }
        if node_id not in deployment_nodes:
            raise RuntimeError("a10_verify_node_not_in_deployment")
        verify = getattr(self._route, "a10_target_verify", None)
        if not callable(verify):
            raise RuntimeError("a10_verify_route_unsupported")
        with self._admission_dispatch_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("route_not_open")
                if self._pending:
                    raise RuntimeError("a10_verify_route_busy")
            if not self._route.is_alive():
                raise RuntimeError("route_not_open")
            result = verify(
                node_id,
                operation=operation,
                deadline_monotonic_s=time.monotonic() + float(timeout_s),
            )
        return unwrap_product_result(operation, result)

    def a10_batch_policy(
        self,
        policy: str,
        *,
        timeout_s: float,
        window_ms: int | None = None,
    ) -> dict[str, Any]:
        """Apply one A10 batch policy to every node of the current deployment.

        The paired benchmark switches between ``baseline`` (runtime batch cap
        one) and ``candidate`` on this same route; ``window_ms`` (candidate only,
        0 through 250) also sets each node's live hop collection window.  The
        switch is serialized with admission, so no request is admitted while the
        nodes change policy, and only each node's own receipt for exactly this
        policy and window is accepted.  A node that refuses fails the switch with
        that node's stable code; the caller must not run a benchmark window on a
        partly switched route.
        """

        if policy not in ("baseline", "candidate"):
            raise ValueError("invalid_a10_batch_policy")
        if window_ms is not None and (
            policy != "candidate"
            or type(window_ms) is not int
            or not 0 <= window_ms <= A10_MAXIMUM_COLLECTION_WINDOW_MS
        ):
            raise ValueError("invalid_a10_collection_window")
        if not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 120:
            raise ValueError("a10_batch_policy_deadline_invalid")
        switch = getattr(self._route, "a10_batch_policy", None)
        if not callable(switch):
            raise RuntimeError("a10_batch_policy_route_unsupported")
        # The route carries the policy string unchanged and accepts only the
        # node's receipt echoing it, so the window travels inside it.
        wire_policy = policy if window_ms is None else f"candidate@{window_ms}"
        node_ids = sorted(
            {
                placement.node_id
                for stage in self._graph.stages
                for placement in stage.placements
            }
        )
        deadline = time.monotonic() + float(timeout_s)
        receipts: dict[str, dict[str, Any]] = {}
        with self._admission_dispatch_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("route_not_open")
            if not self._route.is_alive():
                raise RuntimeError("route_not_open")
            for node_id in node_ids:
                receipt = switch(
                    node_id,
                    policy=wire_policy,
                    deadline_monotonic_s=deadline,
                )
                window = (
                    receipt.get("collection_window_ms")
                    if isinstance(receipt, Mapping)
                    else None
                )
                if (
                    not isinstance(receipt, Mapping)
                    or receipt.get("policy") != wire_policy
                    or type(receipt.get("maximum_runtime_batch_size")) is not int
                    or isinstance(window, bool)
                    or not isinstance(window, (int, float))
                    or (window_ms is not None and window != window_ms)
                ):
                    raise RuntimeError("a10_batch_policy_response_invalid")
                receipts[node_id] = {
                    "maximum_runtime_batch_size": receipt["maximum_runtime_batch_size"],
                    "collection_window_ms": window,
                }
        return {"policy": policy, "window_ms": window_ms, "nodes": receipts}

    def admit(
        self,
        request: RequestContext,
        client_sink: object,
        *,
        pinned_deployment: ExecutionGraph | None = None,
        **kwargs: object,
    ) -> str:
        with self._admission_dispatch_lock:
            return self._admit_serialized(
                request,
                client_sink,
                pinned_deployment=pinned_deployment,
                **kwargs,
            )

    def _admit_serialized(
        self,
        request: RequestContext,
        client_sink: object,
        *,
        pinned_deployment: ExecutionGraph | None = None,
        **kwargs: object,
    ) -> str:
        if not self._route.is_alive():
            raise RuntimeError("route_not_open")

        speculation_preference = kwargs.get("speculation_preference")
        if speculation_preference is not None and (
            not isinstance(speculation_preference, str)
            or speculation_preference not in {"target_only", "speculative_requested"}
        ):
            raise M16AdmissionError("invalid_speculation_preference")
        pending = _Pending(request=request, speculation_preference=speculation_preference)
        coordinator = self._coordinator
        if coordinator is not None:
            profile = kwargs.get("workload_profile_id")
            if profile is None:
                profile = (
                    "sustained_batch_v1"
                    if request.qos_class == "batch"
                    else "interactive_chat_v1"
                )
            if not isinstance(profile, str):
                raise M16AdmissionError("workload_not_qualified")
            excluded_placements = kwargs.get("excluded_placements", frozenset())
            if not isinstance(excluded_placements, frozenset) or not all(
                isinstance(item, str) and item for item in excluded_placements
            ):
                raise M16AdmissionError("invalid_excluded_placements")
            manifest = coordinator.admit(
                request,
                workload_profile_id=profile,
                excluded_placements=excluded_placements,
            )
            pending.placement_ids = tuple(
                hop.placement_id for hop in manifest.ordered_hops
            )
            pending.route_identity = coordinator.route_identity(request.request_id)
            pending.path_manifest = coordinator.path_manifest(request.request_id)
            qualification = kwargs.get("qualification_binding")
            qualification_digest = getattr(
                qualification,
                "qualification_digest",
                None,
            )
            if qualification_digest is None:
                if getattr(self._route, "is_simulated", False) is not True:
                    coordinator.cancel(request.request_id)
                    raise M16AdmissionError("qualification_unavailable")
                qualification_digest = self._digest_document(
                    {
                        "deployment_id": self._graph.deployment_id,
                        "simulation_only": True,
                    }
                )
            with self._lock:
                bound = self._recovery_qualification_digest
                if bound not in (None, qualification_digest) and not (
                    # The route owner replaced the route (cold restart) and
                    # publishes the replacement's qualification as current
                    # authority: rotate, as an idle renewal does.
                    self._rotate_binding_locked(qualification_digest, expected_bound=bound)
                ):
                    coordinator.cancel(request.request_id)
                    raise M16AdmissionError("qualification_binding_changed")
                self._recovery_qualification_digest = qualification_digest
            issued_at_ms = int(time.monotonic() * 1_000)
            publisher_generation = kwargs.get("publisher_generation", 1)
            if type(publisher_generation) is not int or publisher_generation < 1:
                coordinator.cancel(request.request_id)
                raise M16AdmissionError("publisher_generation_invalid")
            first_placement = manifest.ordered_hops[0].placement_id
            graph_placement = next(
                (
                    (stage, placement)
                    for stage in self._graph.stages
                    for placement in stage.placements
                    if placement.placement_id == first_placement
                ),
                None,
            )
            if graph_placement is None:
                coordinator.cancel(request.request_id)
                raise M16AdmissionError("path_identity_invalid")
            stage, placement = graph_placement
            route_identity = pending.route_identity
            assert route_identity is not None
            command_id = f"request:{request.request_id}:attempt:{route_identity['request_attempt']}"
            identity = CommandIdentity(
                deployment_id=route_identity["deployment_id"],
                deployment_epoch=route_identity["deployment_epoch"],
                qualification_digest=qualification_digest,
                request_id=request.request_id,
                request_attempt=route_identity["request_attempt"],
                path_id=route_identity["path_id"],
                path_attempt=route_identity["path_attempt"],
                path_digest=route_identity["path_manifest_digest"],
                topology_generation=route_identity["topology_generation"],
                command_id=command_id,
                publisher_generation=publisher_generation,
                absolute_deadline_ms=issued_at_ms + 3_600_000,
            )
            cleanup_owner_id = f"physical-live-route:{self._graph.deployment_id}"
            idempotency_digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        pending.route_identity,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
            envelope = CommandEnvelope(
                identity=identity,
                stage_id=stage.stage_id,
                placement_id=placement.placement_id,
                assignment_id=placement.assignment_id,
                kind=CommandKind.PREFILL,
                issued_at_ms=issued_at_ms,
                idempotency_digest=idempotency_digest,
                cleanup_owner_id=cleanup_owner_id,
                maximum_request_bytes=131_072,
                maximum_response_bytes=16_777_216,
            )
            validate_interruptible_stage_command(
                {
                    "protocol": "mycelium.interruptible_stage_command.v1",
                    "deployment_id": identity.deployment_id,
                    "deployment_epoch": identity.deployment_epoch,
                    "qualification_digest": identity.qualification_digest,
                    "request_id": identity.request_id,
                    "request_attempt": identity.request_attempt,
                    "path_id": identity.path_id,
                    "path_attempt": identity.path_attempt,
                    "path_digest": identity.path_digest,
                    "topology_generation": identity.topology_generation,
                    "command_id": identity.command_id,
                    "stage_id": envelope.stage_id,
                    "placement_id": envelope.placement_id,
                    "assignment_id": envelope.assignment_id,
                    "command_kind": envelope.kind.value,
                    "issued_at_ms": envelope.issued_at_ms,
                    "idempotency_digest": envelope.idempotency_digest,
                    "cancellation_generation": identity.cancellation_generation,
                    "publisher_generation": identity.publisher_generation,
                    "absolute_deadline_ms": identity.absolute_deadline_ms,
                    "cooperative_step_ms": 100,
                    "cleanup_owner_id": envelope.cleanup_owner_id,
                    "maximum_request_bytes": envelope.maximum_request_bytes,
                    "maximum_response_bytes": envelope.maximum_response_bytes,
                    "expected_terminal_revision": envelope.expected_terminal_revision,
                }
            )
            try:
                with self._lock_order.scope(
                    "deployment",
                    owner_id=request.request_id,
                ):
                    registration = self._commands.register(envelope)
            except LockOrderViolation as error:
                coordinator.cancel(request.request_id)
                raise M16AdmissionError(error.code) from error
            if not registration.accepted:
                coordinator.cancel(request.request_id)
                raise M16AdmissionError("command_registration_failed")
            pending.command_identity = identity
            pending.cleanup_owner_id = cleanup_owner_id
        with self._lock:
            if self._closed:
                if coordinator is not None:
                    coordinator.cancel(request.request_id)
                raise RuntimeError("router_port_closed")
            if request.request_id in self._pending:
                if coordinator is not None:
                    coordinator.cancel(request.request_id)
                raise RuntimeError("duplicate_request_id")
            self._pending[request.request_id] = pending
            self._sinks[request.request_id] = client_sink
            if coordinator is not None:
                self._recovery_current_request_ids.add(request.request_id)
            self._changed.notify_all()

        if coordinator is None:
            self._worker_pool.submit(
                self._run_route, request.request_id, request, pending
            )
        return request.request_id

    def _dispatch_loop(self) -> None:
        coordinator = self._coordinator
        assert coordinator is not None
        while True:
            with self._admission_dispatch_lock:
                with self._changed:
                    if self._closed:
                        return
                    request_id = coordinator.next_dispatch()
                    if request_id is None:
                        self._changed.wait(timeout=0.05)
                        continue
                    pending = self._pending.get(request_id)
                    stored = None if pending is None else pending.request
            if pending is None or stored is None:
                coordinator.complete(request_id, state="failed")
                continue
            self._worker_pool.submit(
                self._run_route, request_id, stored, pending
            )

    def _route_has_scoped_incident_for(self, request: RequestContext | None) -> bool:
        if request is None:
            return False
        # A restarting owner answers attribute reads by raising its restart
        # error; an unreadable route has no incident for this request.
        try:
            incidents = getattr(self._route, "_scoped_runtime_incidents", None)
        except Exception:
            return False
        if isinstance(incidents, Iterable):
            for incident in incidents:
                if (
                    isinstance(incident, Mapping)
                    and incident.get("request_id") == request.request_id
                ):
                    return True
        # Route-level cleanup incidents (request_cleanup_unproven, recorded
        # when exact cleanup could not be proven within the original
        # deadline) are equally authoritative: the runtime reservation must
        # retire so the bounded admission slot is not leaked while the SSE
        # terminal stays unpublished (fail-closed terminal, bounded runtime).
        try:
            route_incidents = getattr(self._route, "_incidents", None)
        except Exception:
            return False
        if isinstance(route_incidents, Iterable):
            for incident in route_incidents:
                if (
                    isinstance(incident, Mapping)
                    and incident.get("request_id") == request.request_id
                    and incident.get("state") == "request_cleanup_unproven"
                ):
                    return True
        return False

    def _run_route(
        self,
        request_id: str,
        request: RequestContext,
        pending: _Pending,
    ) -> None:
        coordinator = self._coordinator
        first_token = False
        self_outer = self

        class _Collector:
            def emit(self, token_index: int, token_id: int) -> None:
                nonlocal first_token
                if coordinator is not None:
                    if route_identity is None or not coordinator.authorizes_route_identity(
                        request_id,
                        route_identity,
                    ):
                        return
                    coordinator.mark_phase(
                        request_id,
                        "first_token" if not first_token else "decode",
                    )
                    first_token = True
                with self_outer._changed:
                    pending.tokens.append((token_index, token_id))
                    self_outer._changed.notify_all()

        terminal_error_code: str | None = None
        try:
            with self._changed:
                pending.execution_started = True
                placement_ids = pending.placement_ids
                route_identity = (
                    None
                    if pending.route_identity is None
                    else dict(pending.route_identity)
                )
                path_manifest = (
                    None
                    if pending.path_manifest is None
                    else dict(pending.path_manifest)
                )
                identity = pending.command_identity
                speculation_preference = pending.speculation_preference
            route_options: dict[str, object] = {}
            if speculation_preference is not None:
                # Only when stated, so a route without A11 keeps its plain
                # signature.  The recovery attempts below never carry it.
                route_options["speculation_preference"] = speculation_preference
            if placement_ids is not None and path_manifest is None:
                route_options["selected_placement_ids"] = placement_ids
            if route_identity is not None:
                route_options["route_identity"] = route_identity
            if path_manifest is not None:
                route_options["locked_path_manifest"] = path_manifest
            if identity is not None:
                route_options["command_identity"] = {
                    "deployment_id": identity.deployment_id,
                    "deployment_epoch": identity.deployment_epoch,
                    "qualification_digest": identity.qualification_digest,
                    "request_id": identity.request_id,
                    "request_attempt": identity.request_attempt,
                    "path_id": identity.path_id,
                    "path_attempt": identity.path_attempt,
                    "path_digest": identity.path_digest,
                    "topology_generation": identity.topology_generation,
                    "command_id": identity.command_id,
                    "publisher_generation": identity.publisher_generation,
                    "absolute_deadline_ms": identity.absolute_deadline_ms,
                    "cancellation_generation": identity.cancellation_generation,
                }
                route_options["authorize_cleanup"] = lambda deadline: (
                    self_outer._request_command_cancellation(
                        pending,
                        deadline_monotonic_s=deadline,
                        completion_cleanup=True,
                    )
                )
                route_options["publish_cleanup_receipt"] = lambda receipt: (
                    self_outer._publish_route_cleanup_receipt(
                        pending, receipt, expected_identity=identity,
                    )
                )
            self._route.infer(
                request.prompt_token_ids,
                max_new_tokens=request.max_new_tokens,
                request_id=request_id,
                sink=_Collector(),
                cancel_requested=pending.cancellation_requested.is_set,
                **route_options,
            )
        except InferenceCancelled:
            terminal = "CANCELLED"
            admission_refused = False
        except AffectedPeerQuarantined as error:
            terminal = "FAILED"
            admission_refused = True
            terminal_error_code = self._bounded_error_code(error)
        except Exception as error:
            terminal = "FAILED"
            admission_refused = False
            terminal_error_code = self._bounded_error_code(error)
        else:
            # Cancellation is linearized by the command controller before the
            # physical route is interrupted.  The route can still return its
            # completed result in the narrow window between that CAS and its
            # local cancellation check.  Do not then try to publish COMPLETED
            # for a command whose cancellation generation already advanced:
            # the controller correctly rejects that transition and the M16
            # reservation would otherwise remain in cleanup forever despite
            # an exact route cleanup receipt.
            terminal = "CANCELLED" if pending.cancellation_linearized else "COMPLETED"
            admission_refused = False
        if pending.cancellation_linearized and pending.cleanup_receipt is not None:
            # The route can publish exact physical cleanup before its original
            # inference command unwinds.  Any later return or exception belongs
            # to the retired generation and cannot override the already-sealed
            # cancellation terminal.
            terminal = "CANCELLED"
            terminal_error_code = None
            admission_refused = False
        recovery_requested = bool(
            terminal == "FAILED"
            and not admission_refused
            and coordinator is not None
            and self._recovery_authority is not None
            and self._route_has_scoped_incident_for(pending.request)
        )
        if recovery_requested:
            assert coordinator is not None
            coordinator.mark_phase(request_id, "cleanup")
            try:
                self._record_command_terminal(pending, "FAILED")
            except RuntimeError as error:
                coordinator.complete(request_id, state="failed")
                with self._changed:
                    pending.terminal_blocked_reason = str(error)
                    self._changed.notify_all()
                return
            with self._changed:
                # This is the source command whose actual route incident made
                # recovery necessary. Keep it after the gateway consumes the
                # transient RECOVERY_REQUIRED state and replaces the command.
                if pending.command_identity is not None:
                    pending.source_failure_identity = {
                        field: getattr(pending.command_identity, field) for field in (
                            "deployment_id", "deployment_epoch", "qualification_digest",
                            "request_id", "request_attempt", "path_id", "path_attempt",
                            "path_digest", "topology_generation", "command_id",
                            "publisher_generation", "cancellation_generation",
                        )
                    }
                    self._retain_source_failure_identity(
                        request_id, pending.source_failure_identity)
                pending.recovery_required = True
                pending.recovery_phase = "failure_observed"
                pending.execution_started = False
                self._changed.notify_all()
            return
        if coordinator is not None:
            coordinator.mark_phase(request_id, "cleanup")
        if admission_refused:
            # The route refused admission before any node command was
            # issued, so no command-ledger terminal CAS is owed. Publish
            # the bounded failed terminal directly so the gateway closes
            # the stream with a definitive outcome instead of retaining a
            # nonterminal session (the cleanup-unproven shape).
            terminal_blocked_reason = None
            if coordinator is not None:
                coordinator.complete(request_id, state="failed")
        else:
            try:
                recorded_terminal = self._record_command_terminal(pending, terminal)
                if recorded_terminal == "FAILED" and terminal != "FAILED":
                    terminal_error_code = "deadline_exceeded"
                terminal = recorded_terminal
            except RuntimeError as error:
                terminal_blocked_reason = str(error)
                # Both facts are computed ONCE here: the branch below is chosen by the incident predicate, and
                # the record must name the branch that actually ran (they differ in whether the runtime
                # reservation is retired or retained).
                scoped_incident = self._route_has_scoped_incident_for(pending.request)
                _record_blocked_terminal(
                    route=self._route,
                    request_id=request_id,
                    reason=terminal_blocked_reason,
                    pending=pending,
                    route_error_code=terminal_error_code,
                    scoped_incident=scoped_incident,
                )
                _LOGGER.error(
                    "live route terminal blocked request_id=%s reason=%s route_error_code=%s",
                    request_id,
                    terminal_blocked_reason[:256],
                    terminal_error_code,
                )
                # If a scoped liveness incident already projected the affected
                # track's terminal status (e.g. a participating peer's transport
                # is fatally failed), the scoped incident is the authoritative
                # cleanup projection. Retire the runtime reservation so the
                # request leaves the cleanup phase; the SSE stream will close
                # without a terminal event, which the gateway's scoped-liveness
                # surfaces as the authoritative outcome. Without a scoped
                # incident we keep the fail-closed "cleanup" phase so the
                # operator can see the unproven cleanup.
                if coordinator is not None and scoped_incident:
                    coordinator.complete(request_id, state="failed")
                elif coordinator is not None:
                    # Cleanup-unproven with no scoped incident: the stream
                    # terminal stays unpublished (fail-closed), but the
                    # dispatch slot must retire so this request cannot pin
                    # concurrent dispatch capacity forever (spec A4 §5).
                    coordinator.retire_dispatch_slot(request_id)
            else:
                terminal_blocked_reason = None
                if coordinator is not None:
                    coordinator.complete(request_id, state=terminal.lower())
        should_release = False
        with self._changed:
            pending.terminal_error_code = terminal_error_code
            if terminal_blocked_reason is None:
                pending.terminal_status = terminal
            else:
                pending.terminal_blocked_reason = terminal_blocked_reason
            if pending.release_requested and pending.terminal_status is not None:
                self._pending.pop(request_id, None)
                self._sinks.pop(request_id, None)
                self._retain_terminal_status(request_id, pending.terminal_status, pending)
                should_release = True
            self._changed.notify_all()
        if should_release:
            self._release_route_request(pending)
            self._retire_command(pending)

    def decode_one(self, request_id: str) -> bool:
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            while (
                pending.cursor >= len(pending.tokens)
                and pending.terminal_status is None
                and pending.terminal_blocked_reason is None
                and not pending.recovery_required
            ):
                self._changed.wait(timeout=30.0)
            if pending.cursor >= len(pending.tokens):
                return False
            token_index, token_id = pending.tokens[pending.cursor]
            pending.cursor += 1
            sink = self._sinks[request_id]
        sink.emit(token_index, token_id)
        return True

    def poll_one(self, request_id: str) -> bool:
        """Deliver one available token without waiting for route progress."""

        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None or pending.cursor >= len(pending.tokens):
                return False
            token_index, token_id = pending.tokens[pending.cursor]
            pending.cursor += 1
            sink = cast(TokenSink, self._sinks[request_id])
        sink.emit(token_index, token_id)
        return True

    def request_status(self, request_id: str) -> str:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return "UNKNOWN"
            if pending.recovery_required and pending.cursor >= len(pending.tokens):
                return "RECOVERY_REQUIRED"
            if (
                pending.terminal_status is not None
                and pending.cursor >= len(pending.tokens)
            ):
                return pending.terminal_status
            if pending.terminal_blocked_reason is not None and pending.cursor >= len(
                pending.tokens
            ):
                return "TERMINAL_BLOCKED"
            return "DECODING"

    def terminal_blocked_identity(self, request_id: str) -> tuple[str, int, str, str] | None:
        """Snapshot blocked state and its command together, never a later attempt."""
        with self._lock:
            pending = self._pending.get(request_id)
            if (pending is None or pending.terminal_blocked_reason is None
                    or pending.terminal_status is not None or pending.recovery_required
                    or pending.cursor < len(pending.tokens)):
                return None
            identity = pending.command_identity
            if identity is None or identity.request_id != request_id:
                return None
            return (identity.request_id, identity.request_attempt,
                    identity.path_digest, identity.command_id)

    def terminal_command_identity(self, request_id: str) -> tuple[str, int, str, str] | None:
        """Snapshot the command that owns a published terminal, before release."""
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.terminal_status is None:
                return None
            identity = pending.command_identity
            if identity is None or identity.request_id != request_id:
                return None
            return (identity.request_id, identity.request_attempt,
                    identity.path_digest, identity.command_id)

    def a6_command_authority(self, request_id: str) -> dict[str, Any] | None:
        """CommandController counts for a request's current or retired attempt.

        Read from the live controller while the command is registered, else
        from the snapshot taken immediately before retirement. Unavailable is
        ``None``, never zero.
        """
        with self._lock:
            pending = self._pending.get(request_id)
            identity = None if pending is None else pending.command_identity
            retained = getattr(self, "_a6_retired_command_authority", {}).get(request_id)
        if identity is not None and identity.request_id == request_id:
            live = self._command_authority_snapshot(identity)
            if live is not None:
                return live
        return None if retained is None else dict(retained)

    def _command_authority_snapshot(self, identity: CommandIdentity) -> dict[str, Any] | None:
        snapshots = self._commands.snapshot(
            identity.request_id, request_attempt=identity.request_attempt)
        if not snapshots or any(
            item.identity.request_id != identity.request_id
            or item.identity.request_attempt != identity.request_attempt
            or item.identity.path_digest != identity.path_digest
            for item in snapshots
        ):
            return None
        return {
            "request_id": identity.request_id,
            "request_attempt": identity.request_attempt,
            "registered_command_count": len(snapshots),
            "terminal_cas_count": sum(item.terminal_revision for item in snapshots),
        }

    def _retain_source_failure_identity(
        self, request_id: str, identity: Mapping[str, Any] | None
    ) -> None:
        """Keep one observed source-failure identity past request release.

        Caller holds ``self._lock``. Bounded FIFO: the oldest released
        observation is evicted first; a live pending request never depends on
        this map.
        """
        if identity is None:
            return
        retained = self._retained_source_failures
        retained.pop(request_id, None)
        retained[request_id] = dict(identity)
        while len(retained) > _RETAINED_SOURCE_FAILURES_MAX:
            retained.popitem(last=False)

    def _retain_terminal_status(
        self, request_id: str, status: str | None, pending: _Pending | None = None,
    ) -> None:
        """Keep a released request's terminal status (bounded FIFO).

        Caller holds ``self._lock``. The gateway releases a settled request
        before an observer may poll; without this a request that never needed
        recovery would have no released view at all and observers waiting for
        its terminal would time out.
        """
        if status is None:
            return
        retained = self._retained_terminal_statuses
        retained.pop(request_id, None)
        retained[request_id] = status
        while len(retained) > _RETAINED_SOURCE_FAILURES_MAX:
            retained.popitem(last=False)
        if pending is not None and pending.recovery_attempts > 0:
            # How a recovery ended (bounded flags and codes only), so an
            # observer polling after release still sees which deadline
            # fired and that the terminal was not cleanup-blocked.
            outcomes = self._retained_recovery_outcomes
            outcomes.pop(request_id, None)
            outcomes[request_id] = {
                "recovery_deadline_exceeded": pending.recovery_deadline_exceeded,
                "recovery_deadline_phase": pending.recovery_deadline_phase,
                "recovery_output_finished": pending.recovery_output_finished,
                "terminal_blocked_reason": pending.terminal_blocked_reason,
            }
            while len(outcomes) > _RETAINED_SOURCE_FAILURES_MAX:
                outcomes.popitem(last=False)

    def _retain_recovery_attempts(self, request_id: str, attempts: int) -> None:
        """Keep how many recovery attempts started past request release.

        Caller holds ``self._lock``. A recovery that settles quickly is
        released by the gateway before an observer polls; the released view
        still reports that recovery ran, never its private state.
        """
        retained = self._retained_recovery_attempts
        retained.pop(request_id, None)
        retained[request_id] = attempts
        while len(retained) > _RETAINED_SOURCE_FAILURES_MAX:
            retained.popitem(last=False)

    def _a6_test_only_source_failure_observed(self, binding: dict[str, Any]) -> bool:
        """Read the source recovery transition, not a successor's current status.

        The pending request is authoritative while it exists. Once the gateway
        has released it (a recovery refused synchronously settles and releases
        before an observer can poll), the bounded retained identity answers.
        """
        request_id = binding.get("request_id")
        if not isinstance(request_id, str):
            return False
        with self._lock:
            pending = self._pending.get(request_id)
            identity = (
                self._retained_source_failures.get(request_id)
                if pending is None else pending.source_failure_identity
            )
            return identity is not None and _source_failure_matches(identity, binding)

    def _a6_test_only_source_fault_binding(self, request_id: str) -> dict[str, Any] | None:
        """Detached admitted-command snapshot; no public fault or route mutation.

        Route revalidates the exact identity under its lock when installing and
        again when consuming the constructor-injected test-only capability.
        """
        with self._changed:
            pending = self._pending.get(request_id)
            coordinator = self._coordinator
            if (
                self._closed or pending is None or coordinator is None
                or pending.request is None or pending.request.request_id != request_id
                or pending.command_identity is None or pending.route_identity is None
                or pending.path_manifest is None or pending.placement_ids is None
                or not pending.execution_started or pending.terminal_status is not None
                or pending.terminal_blocked_reason is not None or pending.recovery_required
                or pending.cancellation_linearized or pending.cancellation_requested.is_set()
                or request_id not in self._recovery_current_request_ids
                or not coordinator.authorizes_route_identity(request_id, pending.route_identity)
            ):
                return None
            command = pending.command_identity
            if int(time.monotonic() * 1000) >= command.absolute_deadline_ms:
                return None
            identity = {field: getattr(command, field) for field in (
                "deployment_id", "deployment_epoch", "qualification_digest",
                "request_id", "request_attempt", "path_id", "path_attempt",
                "path_digest", "topology_generation", "command_id",
                "publisher_generation", "cancellation_generation",
            )}
            return {
                **identity, "absolute_deadline_ms": command.absolute_deadline_ms,
                "path_manifest_digest": pending.route_identity["path_manifest_digest"],
                "source_attempt": pending.route_identity["request_attempt"],
                "source_node_id": next((placement.node_id for stage in self._graph.stages
                                        for placement in stage.placements
                                        if placement.placement_id == pending.placement_ids[0]), None),
            }

    # -- A6 controlled physical fault reads ---------------------------------
    #
    # The product-mounted A6 receiver binds its seams through these reads. They
    # are detached snapshots: none mints an identity, advances an attempt, or
    # publishes output. ``a6_withhold_recovery_successors`` is the only write:
    # it narrows the successor candidate set for one owned request, which can
    # only make recovery refuse, never succeed.

    def a6_controlled_fault_binding(self, request_id: str) -> dict[str, Any] | None:
        """Admitted in-flight command identity for a grant-gated product fault."""
        return self._a6_test_only_source_fault_binding(request_id)

    def a6_source_failure_observed(self, binding: dict[str, Any]) -> bool:
        return self._a6_test_only_source_failure_observed(binding)

    def a6_inflight_request_ids(self) -> tuple[str, ...]:
        """Requests executing on their source attempt with no terminal yet."""
        with self._lock:
            return tuple(sorted(
                request_id for request_id, pending in self._pending.items()
                if pending.execution_started and pending.terminal_status is None
                and pending.terminal_blocked_reason is None
                and not pending.recovery_required
                and pending.source_failure_identity is None
                and not pending.cancellation_requested.is_set()
            ))

    def a6_request_view(self, request_id: str) -> dict[str, Any] | None:
        """Privacy-reduced recovery state of one request; no token material."""
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                reason = self._recovery_failures.get(request_id)
                attempts = self._retained_recovery_attempts.get(request_id)
                terminal = self._retained_terminal_statuses.get(request_id)
                if reason is None and attempts is None and terminal is None:
                    return None
                view: dict[str, Any] = {
                    "retained": False, "recovery_failure_reason": reason}
                if terminal is not None:
                    view["terminal_status"] = terminal
                if attempts is not None:
                    view["recovery_attempts"] = attempts
                    view["source_failure_observed"] = (
                        request_id in self._retained_source_failures)
                    outcome = self._retained_recovery_outcomes.get(request_id)
                    if outcome is not None:
                        view.update(outcome)
                return view
            identity = pending.command_identity
            return {
                "retained": True,
                "execution_started": pending.execution_started,
                "terminal_status": pending.terminal_status,
                "terminal_error_code": pending.terminal_error_code,
                "terminal_blocked_reason": pending.terminal_blocked_reason,
                "recovery_required": pending.recovery_required,
                "recovery_phase": pending.recovery_phase,
                "recovery_attempts": pending.recovery_attempts,
                "recovery_failure_reason": pending.recovery_failure_reason,
                "recovery_deadline_exceeded": pending.recovery_deadline_exceeded,
                "recovery_deadline_phase": pending.recovery_deadline_phase,
                "recovery_output_count": pending.recovery_output_count,
                "recovery_output_limit": pending.recovery_output_limit,
                "recovery_output_finished": pending.recovery_output_finished,
                "recovery_cleanup_held": pending.a6_completion_cleanup_held,
                "source_failure_observed": pending.source_failure_identity is not None,
                "cancellation_requested": pending.cancellation_requested.is_set(),
                "cancellation_linearized": pending.cancellation_linearized,
                "token_count": len(pending.tokens),
                "cursor": pending.cursor,
                "placement_ids": tuple(pending.placement_ids or ()),
                "excluded_placement_ids": tuple(sorted(pending.recovery_excluded_placements)),
                "request_attempt": None if identity is None else identity.request_attempt,
                "path_id": None if identity is None else identity.path_id,
                "path_attempt": None if identity is None else identity.path_attempt,
                "path_digest": None if identity is None else identity.path_digest,
                "command_id": None if identity is None else identity.command_id,
                "route_identity": (None if pending.route_identity is None
                                   else dict(pending.route_identity)),
            }

    def a6_placement_node_ids(self) -> dict[str, str]:
        """Placement to node mapping of the current deployment graph."""
        return {placement.placement_id: placement.node_id
                for stage in self._graph.stages for placement in stage.placements}

    def a6_withhold_recovery_successors(self, request_id: str) -> bool:
        """Exclude every placement from this request's successor candidates."""
        placements = set(self.a6_placement_node_ids())
        with self._changed:
            pending = self._pending.get(request_id)
            if (pending is None or not pending.execution_started
                    or pending.terminal_status is not None or not placements):
                return False
            pending.recovery_excluded_placements.update(placements)
            return True

    def a6_arm_completion_cleanup_hold(self, request_id: str) -> bool:
        """Hold this request's successor completion cleanup until released.

        Armed only by the A6 cleanup-authority fault seam, before it fails the
        source, so the seam can stall the successor's peers before their
        completion cleanup can be proven. Unarmed (the default) the
        authorization path never waits.
        """
        with self._changed:
            pending = self._pending.get(request_id)
            if (pending is None or not pending.execution_started
                    or pending.terminal_status is not None
                    or pending.terminal_blocked_reason is not None
                    or pending.recovery_phase != "active"
                    or pending.a6_completion_cleanup_hold is not None):
                return False
            pending.a6_completion_cleanup_hold = threading.Event()
            return True

    def a6_release_completion_cleanup_hold(self, request_id: str) -> bool:
        with self._changed:
            pending = self._pending.get(request_id)
            hold = None if pending is None else pending.a6_completion_cleanup_hold
        if hold is None:
            return False
        hold.set()
        return True

    def request_error_code(self, request_id: str) -> str | None:
        """Return one bounded terminal failure code without private diagnostics."""

        with self._lock:
            pending = self._pending.get(request_id)
            return None if pending is None else pending.terminal_error_code

    def recovery_checkpoint_binding(self, request_id: str) -> dict[str, object]:
        """Return privacy-reduced source authority needed for gateway freeze."""

        with self._changed:
            pending = self._pending.get(request_id)
            if (
                pending is None
                or not pending.recovery_required
                or pending.route_identity is None
                or self._recovery_tokenizer_digest is None
            ):
                raise M16AdmissionError("recovery_not_pending")
            source = dict(pending.route_identity)
            attempt = pending.recovery_attempts
        now_ms = self._recovery_clock_ms()
        return {
            "store_identity": (
                f"a6:{source['deployment_id']}:{request_id}:attempt:{attempt}"
            ),
            "session_id": f"request-session:{request_id}",
            "source_attempt": source["request_attempt"],
            "path_id": source["path_id"],
            "path_digest": source["path_manifest_digest"],
            "path_attempt": source["path_attempt"],
            "tokenizer_digest": self._recovery_tokenizer_digest,
            "created_at_unix_ms": now_ms,
            # The total recovery deadline. Qualification, prefill, progress
            # and cutover have their own shorter bounds (RecoveryPhaseDeadlines).
            "expiry_unix_ms": now_ms + self._recovery_deadlines.total_ms,
            "recovery_attempt": attempt,
        }

    @staticmethod
    def _recovery_failure_code(error: BaseException) -> str:
        """Map internal failures to bounded non-disclosing retained codes."""

        if isinstance(error, (M16AdmissionError, ValueError)) and str(error) in (
            _RECOVERY_EXPIRY_CODES
        ):
            # Owner ruling: a replay checkpoint or successor qualification that
            # expires while recovery is in flight is the recovery timeout.
            return "recovery_timeout"
        if isinstance(error, M16AdmissionError):
            code = str(error)
            if (
                code
                and len(code) <= 96
                and all(character.islower() or character.isdigit() or character == "_" for character in code)
            ):
                return code
            return "recovery_authority_rejected"
        if isinstance(error, InferenceCancelled):
            return "recovery_cancelled"
        if isinstance(error, TimeoutError):
            return "recovery_timeout"
        if isinstance(error, RuntimeError) and str(error) == "command_cleanup_receipt_missing":
            return "cleanup_authority_failed"
        if isinstance(error, RuntimeError):
            return "successor_execution_failed"
        return "recovery_internal_failure"

    def recover_full_context(
        self,
        request_id: str,
        checkpoint: ReplayCheckpoint,
    ) -> bool:
        """Execute one qualified full-context replay and publish only its suffix."""

        coordinator = self._coordinator
        authority = self._recovery_authority
        qualification_source = self._recovery_qualification_source
        if coordinator is None or authority is None or qualification_source is None:
            return False
        with self._changed:
            pending = self._pending.get(request_id)
            if (
                pending is None
                or not pending.recovery_required
                or pending.request is None
                or pending.route_identity is None
                or pending.placement_ids is None
                or pending.recovery_attempts >= 2
            ):
                return False
            source_request = pending.request
            source_identity = dict(pending.route_identity)
            excluded_placements = frozenset(
                set(pending.placement_ids) | pending.recovery_excluded_placements
            )
            source_command = pending.command_identity
            source_token_count = len(pending.tokens)
            publisher_generation = (
                1
                if source_command is None
                else source_command.publisher_generation
            )
            pending.recovery_attempts += 1
            pending.recovery_phase = "successor_pending"
            pending.recovery_last_progress_unix_ms = None
            self._retain_recovery_attempts(request_id, pending.recovery_attempts)
        deadlines = self._recovery_deadlines
        attempt_started_ms = self._recovery_clock_ms()
        #: The phase whose bound applies to a failure that is not the
        #: in-flight guard's own interruption (``execution`` has the guard).
        stage = "qualification"

        successor_identity: dict[str, Any] | None = None
        source_fenced = False
        cutover_committed = False
        successor_registered = False
        successor_placements: tuple[str, ...] = ()
        command_terminal_error: str | None = None
        _LOGGER.info("a6 recovery stage=reserve request_id=%s", request_id)
        try:
            manifest = authority.reserve_candidate(
                checkpoint=checkpoint,
                source_request=source_request,
                expected_source_identity=source_identity,
                excluded_placements=excluded_placements,
                now_unix_ms=self._recovery_clock_ms(),
            )
            successor_identity = coordinator.recovery_successor_identity(request_id)
            _LOGGER.info("a6 recovery stage=qualify request_id=%s", request_id)
            source_compatibility, qualification = qualification_source(
                request_id,
                checkpoint,
                successor_identity,
                manifest,
            )
            authority.qualify_reserved_successor(
                checkpoint=checkpoint,
                successor_manifest=manifest,
                qualification=qualification,
                source_compatibility=source_compatibility,
                expected_source_identity=source_identity,
                now_unix_ms=self._recovery_clock_ms(),
            )
            # The successor qualified, but too late: it is never fenced in.
            self._enforce_recovery_phase_deadline(
                pending, checkpoint, phase="qualification",
                bound_unix_ms=attempt_started_ms + deadlines.qualification_ms)
            successor_request = coordinator.recovery_successor_request(request_id)
            successor_manifest = path_manifest_to_dict(manifest)
            successor_placements = tuple(
                hop.placement_id for hop in manifest.ordered_hops
            )
            if source_command is None:
                raise M16AdmissionError("recovery_command_identity_missing")
            _LOGGER.info("a6 recovery stage=register request_id=%s", request_id)
            self._register_recovery_command(
                pending,
                route_identity=successor_identity,
                placement_ids=successor_placements,
                qualification_digest=source_command.qualification_digest,
                publisher_generation=publisher_generation,
                absolute_deadline_ms=source_command.absolute_deadline_ms,
            )
            successor_registered = True
            # An owner cancellation accepted while the successor was being
            # reserved and qualified ends the recovery before the source is
            # fenced or any successor work starts.
            with self._changed:
                if (pending.cancellation_requested.is_set()
                        or pending.cancellation_linearized):
                    raise InferenceCancelled("recovery_cancelled_before_fence")
            _LOGGER.info("a6 recovery stage=fence request_id=%s", request_id)
            coordinator.fence_recovery_source(
                request_id,
                expected_source_identity=source_identity,
            )
            source_fenced = True

            buffered: list[tuple[int, int]] = []
            recovery_clock_ms = self._recovery_clock_ms

            class _RecoveryCollector:
                def emit(self, token_index: int, token_id: int) -> None:
                    if (
                        type(token_index) is not int
                        or token_index != len(buffered)
                        or type(token_id) is not int
                        or token_id < 0
                    ):
                        raise M16AdmissionError("recovery_output_noncontiguous")
                    buffered.append((token_index, token_id))
                    pending.recovery_output_count = len(buffered)
                    # Progress re-arms the idle (progress) deadline.
                    pending.recovery_last_progress_unix_ms = recovery_clock_ms()

            identity = pending.command_identity
            assert identity is not None
            route_options = {
                "route_identity": successor_identity,
                "locked_path_manifest": successor_manifest,
                "command_identity": self._command_identity_document(identity),
                "publish_cleanup_receipt": lambda receipt: (
                    self._publish_route_cleanup_receipt(
                        pending, receipt, expected_identity=identity,
                    )
                ),
                "authorize_cleanup": lambda deadline: (
                    self._authorize_recovery_cleanup(pending, deadline)
                ),
            }
            self._release_predecessor_attempt(pending, source_command)
            with self._changed:
                pending.execution_started = True
                pending.recovery_output_count = 0
                pending.recovery_output_limit = successor_request.max_new_tokens
                pending.recovery_output_finished = False
                pending.recovery_phase = "recovery_prefill"
                pending.recovery_last_progress_unix_ms = None
            stage = "execution"
            execution_started_ms = self._recovery_clock_ms()

            def execution_deadline() -> tuple[int, str]:
                # Prefill until the first successor token, then the idle
                # bound since the latest token; never past the total cap.
                last_progress = pending.recovery_last_progress_unix_ms
                if last_progress is None:
                    bound, phase = execution_started_ms + deadlines.prefill_ms, "prefill"
                else:
                    bound, phase = last_progress + deadlines.progress_ms, "progress"
                if checkpoint.expiry_unix_ms <= bound:
                    return checkpoint.expiry_unix_ms, "total"
                return bound, phase

            deadline_guard = _RecoveryDeadlineGuard(
                deadline=execution_deadline,
                clock_ms=self._recovery_clock_ms,
                interrupt=lambda stopped, phase: self._interrupt_recovery_at_deadline(
                    pending, request_id, stopped=stopped, phase=phase,
                    deadline=execution_deadline),
            )
            execution_error: Exception | None = None
            try:
                self._route.infer(
                    successor_request.prompt_token_ids,
                    max_new_tokens=successor_request.max_new_tokens,
                    request_id=request_id,
                    sink=_RecoveryCollector(),
                    cancel_requested=pending.cancellation_requested.is_set,
                    **route_options,
                )
            except Exception as error:
                execution_error = error
            finally:
                with self._changed:
                    # Under the router lock: the guard either interrupted the
                    # work above or can no longer fire.
                    deadline_guard.stop()
            if pending.recovery_deadline_exceeded:
                # The interrupted worker can return before the interruption's
                # cancellation fan-out has published the successor's exact
                # cleanup receipt. Wait (bounded by the cancellation budget)
                # for it so the abort records a proven-clean terminal instead
                # of ``command_cleanup_receipt_missing``.
                deadline_guard.join(_MAX_CANCELLATION_AND_CLEANUP_SECONDS)
                raise TimeoutError("recovery_deadline_exceeded") from execution_error
            if execution_error is not None:
                raise execution_error
            if len(buffered) > successor_request.max_new_tokens:
                raise M16AdmissionError("recovery_output_overflow")
            stage = "cutover"
            output_finished_ms = self._recovery_clock_ms()

            # Qualification is an expiring owner publication, not a one-time
            # prefill admission token. Re-read and validate the exact reserved
            # successor immediately before the cutover CAS so expiry,
            # revocation, deployment, topology, or path drift cannot authorize
            # publication after long successor execution.
            current_source_compatibility, current_qualification = (
                qualification_source(
                    request_id,
                    checkpoint,
                    successor_identity,
                    manifest,
                )
            )
            authority.qualify_reserved_successor(
                checkpoint=checkpoint,
                successor_manifest=manifest,
                qualification=current_qualification,
                source_compatibility=current_source_compatibility,
                expected_source_identity=source_identity,
                now_unix_ms=self._recovery_clock_ms(),
            )

            # Cancellation and cutover share the Router lock. Once an accepted
            # cancellation is visible, no successor publication generation may
            # be committed behind it.
            with self._changed:
                if pending.cancellation_requested.is_set():
                    raise InferenceCancelled("recovery_cancelled_before_cutover")
                self._enforce_recovery_phase_deadline(
                    pending, checkpoint, phase="cutover",
                    bound_unix_ms=output_finished_ms + deadlines.cutover_ms)
                pending.recovery_phase = "cutover_pending"
                committed_identity = coordinator.cutover_recovery_successor(
                    request_id,
                    expected_source_identity=source_identity,
                    expected_successor_identity=successor_identity,
                )
                cutover_committed = True
            with self._changed:
                pending.request = successor_request
                pending.route_identity = committed_identity
                pending.path_manifest = successor_manifest
                pending.placement_ids = successor_placements
                for token_index, token_id in buffered:
                    coordinator.mark_phase(
                        request_id,
                        "first_token" if token_index == 0 else "decode",
                    )
                    pending.tokens.append(
                        (
                            checkpoint.committed_token_count + token_index,
                            token_id,
                        )
                    )
                pending.recovery_required = False
                pending.recovery_phase = "recovered_decode"
            cutover_observer = getattr(self._route, "recovery_cutover_committed", None)
            if callable(cutover_observer):
                cutover_observer(request_id)
            coordinator.mark_phase(request_id, "cleanup")
            terminal = self._record_command_terminal(pending, "COMPLETED")
            coordinator.complete(request_id, state=terminal.lower())
            with self._changed:
                pending.terminal_status = terminal
                if terminal == "FAILED":
                    pending.terminal_error_code = "deadline_exceeded"
                self._changed.notify_all()
            return True
        except Exception as error:
            retryable_successor_failure = (
                source_fenced
                and not cutover_committed
                and successor_identity is not None
                and isinstance(error, (RuntimeError, TimeoutError))
                and not isinstance(error, (InferenceCancelled, M16AdmissionError))
                and not pending.cancellation_requested.is_set()
                and pending.recovery_attempts < 2
                # The checkpoint expired: no further attempt can qualify.
                and not pending.recovery_deadline_exceeded
            )
            if retryable_successor_failure:
                try:
                    if successor_registered:
                        self._record_command_terminal(pending, "FAILED")
                        self._retire_command(pending)
                        successor_registered = False
                    coordinator.discard_recovery_successor(
                        request_id,
                        expected_source_identity=source_identity,
                    )
                except Exception:
                    retryable_successor_failure = False
                else:
                    with self._changed:
                        pending.recovery_excluded_placements.update(
                            successor_placements
                        )
                        pending.recovery_phase = "successor_pending"
                        self._changed.notify_all()
                    return self.recover_full_context(request_id, checkpoint)

            if (
                stage != "execution"
                and not isinstance(error, InferenceCancelled)
                and self._recovery_failure_code(error) == "recovery_timeout"
            ):
                # An authority or qualifier expiry outside successor
                # execution: name the bound it belongs to.
                with self._changed:
                    if pending.recovery_deadline_phase is None:
                        pending.recovery_deadline_phase = (
                            "total"
                            if self._recovery_clock_ms() >= checkpoint.expiry_unix_ms
                            else stage
                        )

            # Which cancellation evidence ends the recovery: each condition is
            # recorded, because a recovery nobody cancelled must never end in
            # the cancelled branch (A6 case 15 on c0dc4350).
            cancelled_conditions = {
                "inference_cancelled": isinstance(error, InferenceCancelled),
                "cancellation_requested": pending.cancellation_requested.is_set(),
                "coordinator_cancelled": (
                    coordinator.terminal_state(request_id) == "cancelled"),
            }
            cancelled = any(cancelled_conditions.values())
            command_generation_at_failure = (
                None if pending.command_identity is None
                else pending.command_identity.cancellation_generation)
            try:
                if cancelled:
                    if coordinator.terminal_state(request_id) is None:
                        coordinator.cancel(request_id)
                elif cutover_committed:
                    # Cutover moved M16 authority to the successor, but no
                    # terminal may be published until exact cleanup is proven.
                    # Keep the coordinator nonterminal in cleanup when the
                    # command cleanup receipt/terminal CAS fails.
                    pass
                elif source_fenced and successor_identity is not None:
                    coordinator.abort_recovery(
                        request_id,
                        expected_source_identity=source_identity,
                        expected_successor_identity=successor_identity,
                    )
                else:
                    if successor_identity is not None:
                        coordinator.discard_recovery_successor(
                            request_id,
                            expected_source_identity=source_identity,
                        )
                    if coordinator.terminal_state(request_id) is None:
                        coordinator.complete(request_id, state="failed")
            finally:
                if successor_registered:
                    try:
                        self._record_command_terminal(
                            pending,
                            "CANCELLED" if cancelled else "FAILED",
                        )
                    except RuntimeError as terminal_error:
                        command_terminal_error = str(terminal_error)
                with self._changed:
                    pending.recovery_required = False
                    if cutover_committed:
                        del pending.tokens[source_token_count:]
                        pending.cursor = min(pending.cursor, len(pending.tokens))
                    if cancelled:
                        pending.recovery_failure_reason = None
                        if command_terminal_error is None:
                            pending.terminal_status = "CANCELLED"
                    else:
                        pending.recovery_failure_reason = self._recovery_failure_code(error)
                        self._recovery_failures[request_id] = (
                            pending.recovery_failure_reason
                        )
                        if command_terminal_error is None:
                            pending.terminal_status = "FAILED"
                    if command_terminal_error is not None:
                        pending.terminal_blocked_reason = command_terminal_error
                    self._changed.notify_all()
            # The route's own failure underneath a cleanup-unproven refusal,
            # so a blocked successor says what it actually hit.
            cause = error.__cause__
            diagnostics = {
                "request_id": request_id,
                "error_type": type(error).__name__,
                "error_code": self._bounded_error_code(error),
                "route_failure_code": (
                    None if cause is None else self._bounded_error_code(cause)),
                "source_fenced": source_fenced,
                "successor_reserved": successor_identity is not None,
                "successor_placement_ids": list(successor_placements),
                "recovery_attempts": pending.recovery_attempts,
                "terminal_blocked_reason": (
                    None
                    if command_terminal_error is None
                    else command_terminal_error[:96]
                ),
            }
            if not cancelled:
                # Operator diagnostics only: bounded codes, no request content.
                _LOGGER.warning(
                    "%s",
                    json.dumps(
                        {
                            "event": "a6_recovery_failed",
                            "reason": pending.recovery_failure_reason,
                            **diagnostics,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            else:
                _LOGGER.warning(
                    "%s",
                    json.dumps(
                        {
                            "event": "a6_recovery_cancelled",
                            "conditions": [name for name, fired in
                                           cancelled_conditions.items() if fired],
                            "cancellation_linearized": pending.cancellation_linearized,
                            "cutover_committed": cutover_committed,
                            "command_cancellation_generation": (
                                command_generation_at_failure),
                            **diagnostics,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            if command_terminal_error is not None:
                _record_blocked_terminal(
                    route=self._route,
                    request_id=request_id,
                    reason=command_terminal_error,
                    pending=pending,
                    route_error_code=diagnostics["route_failure_code"]
                    or diagnostics["error_code"],
                    scoped_incident=False,
                )
            return False

    def _enforce_recovery_phase_deadline(
        self,
        pending: _Pending,
        checkpoint: ReplayCheckpoint,
        *,
        phase: str,
        bound_unix_ms: int,
    ) -> None:
        """Fail the recovery if ``phase`` (or the total cap) has elapsed.

        Used at the phase boundaries no successor work is in flight for:
        qualification before fencing, cutover before the CAS.
        """

        now_ms = self._recovery_clock_ms()
        if now_ms >= checkpoint.expiry_unix_ms:
            phase = "total"
        elif now_ms < bound_unix_ms:
            return
        with self._changed:
            pending.recovery_deadline_exceeded = True
            pending.recovery_deadline_phase = phase
            self._changed.notify_all()
        _LOGGER.warning("%s", json.dumps(
            {"event": "a6_recovery_deadline_exceeded",
             "request_id": pending.request.request_id if pending.request else None,
             "phase": phase, "recovery_attempts": pending.recovery_attempts},
            sort_keys=True, separators=(",", ":")))
        raise TimeoutError("recovery_deadline_exceeded")

    def _interrupt_recovery_at_deadline(
        self,
        pending: _Pending,
        request_id: str,
        *,
        stopped: Callable[[], bool],
        phase: str,
        deadline: Callable[[], tuple[int, str]],
    ) -> bool | None:
        """Interrupt the successor command whose recovery deadline elapsed.

        Uses the owner cancellation mechanics (command-controller cancellation
        generation, then physical fan-out) without setting the owner's
        cancellation latch, so the request fails as a recovery timeout.
        Returns ``None`` when the bound moved (a successor token arrived)
        since the guard read it, so the guard keeps watching.
        """

        route_cancel = None
        with self._changed:
            if (stopped()
                    or pending.cancellation_requested.is_set()
                    or pending.terminal_status is not None
                    or pending.recovery_phase != "recovery_prefill"):
                return False
            bound_unix_ms, phase = deadline()
            if self._recovery_clock_ms() < bound_unix_ms:
                return None
            pending.recovery_deadline_exceeded = True
            pending.recovery_deadline_phase = phase
            cancellation_deadline = self._request_command_cancellation(
                pending,
                deadline_monotonic_s=(
                    time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS),
            )
            route_cancel = getattr(self._route, "cancel_request", None)
            self._changed.notify_all()
        _LOGGER.warning("%s", json.dumps(
            {"event": "a6_recovery_deadline_exceeded", "request_id": request_id,
             "phase": phase, "recovery_attempts": pending.recovery_attempts},
            sort_keys=True, separators=(",", ":")))
        if callable(route_cancel):
            route_cancel(request_id, deadline_monotonic_s=cancellation_deadline)
        return True

    def _rotate_binding_locked(self, candidate: str, *, expected_bound: str) -> bool:
        """Rotate the bound digest to ``candidate``; caller holds ``self._lock``.

        The check and the mutation are one critical section: the source's
        current record is re-read here, under the lock, so a record published
        (or a route replaced again) after any earlier read can never be bound.
        Refused unless the binding is still ``expected_bound``, no admitted
        request would cross the boundary, and ``candidate`` is exactly the
        source's non-refreshing current record for this deployment and graph.
        Lock order: router lock, then the source's own short lock (the owner
        never calls back into the router while holding its lock).
        """

        if (
            self._recovery_qualification_digest != expected_bound
            or self._pending
            or self._replacement_binding_digest(candidate) != candidate
        ):
            return False
        self._carry_terminal_requests_across_binding_locked()
        self._recovery_qualification_digest = candidate
        return True

    def _carry_terminal_requests_across_binding_locked(self) -> None:
        """Start a new binding's request set, keeping finished requests visible.

        Caller holds ``self._lock`` and changes the binding only with no
        admitted request, so every listed request has been released; only
        those released with a terminal status are carried (in the order they
        finished), never in-flight state. Carried records stay this router's
        own deployment and graph (a binding changes only within them) and
        report the same terminal facts, never a new admission.
        """

        current = self._recovery_current_request_ids
        if not current:
            return  # nothing to carry; projection reads prune the carried set
        now_ms = self._recovery_clock_ms()
        carried = self._recovery_rotated_terminal_ids
        for request_id in self._retained_terminal_statuses:
            if request_id in current:
                carried.pop(request_id, None)
                carried[request_id] = now_ms
        current.clear()
        self._prune_rotated_terminal_requests_locked(now_ms)

    def _prune_rotated_terminal_requests_locked(self, now_ms: int) -> None:
        """Drop carried records past the age bound, then the oldest past the count."""

        carried = self._recovery_rotated_terminal_ids
        for request_id, carried_ms in list(carried.items()):
            if not 0 <= now_ms - carried_ms <= _ROTATED_TERMINAL_RECORD_MAX_AGE_MS:
                carried.pop(request_id)
        while len(carried) > _ROTATED_TERMINAL_RECORDS_MAX:
            carried.popitem(last=False)

    def _replacement_binding_digest(self, candidate: str | None) -> str | None:
        """The candidate digest iff it is the qualifier's current authority.

        After a cold route restart the owner qualifies the replacement route
        and publishes it as current; the bound digest still names the retired
        route. Only the source's side-effect-free ``peek_current`` snapshot may
        rotate the binding (a refreshing ``current()`` could run a challenge
        or renew authority), and only for this exact deployment and graph.
        """

        source = self._qualification_source
        if source is None or not isinstance(candidate, str):
            return None
        reader = getattr(source, "peek_current", None)
        if not callable(reader):
            return None
        try:
            record = reader()
        except Exception:
            return None
        from mycelium_qualification.contracts import RouteQualificationV1
        from mycelium_request_gateway.contracts import qualification_digest

        if (
            not isinstance(record, RouteQualificationV1)
            or record.route_ready is not True
            or record.deployment_id != self._graph.deployment_id
            or record.deployment_epoch != self._graph.deployment_epoch
            or record.topology_version != self._graph.topology_version
            or record.model_id != self._graph.model_id
            or record.resolved_commit != self._graph.resolved_commit
            or record.manifest_digest != self._graph.manifest_digest
        ):
            return None
        try:
            current = qualification_digest(record)
        except Exception:
            return None
        return candidate if current == candidate else None

    def _recovery_in_flight_locked(self) -> bool:
        """A visible admitted request is failing over; caller holds ``self._lock``."""

        return any(
            request_id in self._recovery_current_request_ids
            and getattr(pending, "terminal_status", None) is None
            and getattr(pending, "recovery_phase", "active") != "active"
            for request_id, pending in self._pending.items()
        )

    def _recovery_projection_qualification_digest(self) -> str:
        """Resolve the current qualifier binding for the public projection.

        A bound digest may describe an earlier admission epoch. If a source is
        wired, its non-refreshing authority snapshot must agree with the bound
        digest; absent, expired, revoked or foreign authority fails closed.
        While an admitted request fails over, the projection stays bound to
        the digest that request was admitted under. Source-less simulated
        legacy adapters retain their admitted binding.
        """

        with self._lock:
            bound = self._recovery_qualification_digest
            recovering = bound is not None and self._recovery_in_flight_locked()
        source = self._qualification_source
        if source is None:
            if bound is not None:
                return bound  # legacy simulated router without a source
            raise M16AdmissionError("recovery_projection_unavailable")
        if recovering:
            # A source failure withdraws or replaces the route's qualification
            # while its admitted request fails over. That request stays bound
            # to the qualification it was admitted under, and so does the
            # projection that reports its recovery.
            return bound
        # A physical owner must not run a model challenge on an HTTP status
        # read. Its snapshot is non-refreshing; the admission path renews.
        reader = getattr(source, "peek_current", None)
        if not callable(reader):
            reader = source.current
        try:
            record = reader()
        except Exception:
            raise M16AdmissionError("recovery_projection_unavailable") from None
        from mycelium_qualification.contracts import RouteQualificationV1

        if record is None or not isinstance(record, RouteQualificationV1):
            raise M16AdmissionError("recovery_projection_unavailable")
        if record.route_ready is not True:
            raise M16AdmissionError("recovery_projection_unavailable")
        if (
            record.deployment_id != self._graph.deployment_id
            or record.deployment_epoch != self._graph.deployment_epoch
        ):
            raise M16AdmissionError("recovery_projection_binding_mismatch")
        from mycelium_request_gateway.contracts import qualification_digest

        try:
            digest = qualification_digest(record)
        except Exception:
            raise M16AdmissionError("recovery_projection_binding_invalid") from None
        if (
            not isinstance(digest, str)
            or len(digest) != 71
            or not digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in digest[7:])
        ):
            raise M16AdmissionError("recovery_projection_binding_invalid")
        if bound is not None and digest != bound:
            with self._lock:
                # Idle after a route replacement: the projection binds the
                # replacement's current qualification, as before any admission.
                if not self._rotate_binding_locked(digest, expected_bound=bound):
                    raise M16AdmissionError("recovery_projection_binding_mismatch")
        return digest

    def recovery_public_projection(
        self, committed_token_counts: Mapping[str, int]
    ) -> dict[str, Any]:
        """Aggregate Router facts with gateway-owned committed counts."""

        coordinator = self._coordinator
        atomic_commit = self._recovery_a4_atomic_commit
        if coordinator is None or atomic_commit is None:
            raise M16AdmissionError("recovery_projection_unavailable")
        qualification_digest = self._recovery_projection_qualification_digest()
        with self._lock:
            if self._recovery_qualification_digest not in (None, qualification_digest):
                raise M16AdmissionError("recovery_projection_binding_mismatch")
            self._prune_rotated_terminal_requests_locked(self._recovery_clock_ms())
            visible_request_ids = frozenset(self._recovery_current_request_ids)
            carried_request_ids = frozenset(self._recovery_rotated_terminal_ids)
        if qualification_digest is None:
            raise M16AdmissionError("recovery_projection_unavailable")
        if (
            not isinstance(atomic_commit, str)
            or len(atomic_commit) != 40
            or any(character not in "0123456789abcdef" for character in atomic_commit)
        ):
            raise M16AdmissionError("recovery_projection_binding_invalid")
        status = coordinator.status()
        requests: list[dict[str, Any]] = []
        for item in status["requests"]:
            request_id = item["request_id"]
            if request_id not in committed_token_counts:
                continue
            if request_id not in visible_request_ids and not (
                # Finished under an earlier binding of this deployment: listed
                # with its terminal facts, never as live recovery state.
                request_id in carried_request_ids and item["terminal_state"] is not None
            ):
                continue
            committed_count = committed_token_counts[request_id]
            if type(committed_count) is not int or committed_count < 0:
                raise M16AdmissionError("recovery_projection_count_invalid")
            identity = coordinator.route_identity(request_id)
            terminal = item["terminal_state"]
            if terminal is None:
                with self._lock:
                    pending = self._pending.get(request_id)
                    phase = "active" if pending is None else pending.recovery_phase
            else:
                phase = "terminal" if terminal == "completed" else "aborted"
            requests.append(
                {
                    "request_id": request_id,
                    "attempt": identity["request_attempt"],
                    "path_id": identity["path_id"],
                    "path_attempt": identity["path_attempt"],
                    "phase": phase,
                    "committed_token_count": committed_count,
                    "recovery_attempt": max(0, identity["request_attempt"] - 1),
                    "cleanup_complete": terminal is not None and item["reservation_count"] == 0,
                    "reconciled": None,
                    "terminal": terminal,
                }
            )
        requests.sort(key=lambda item: item["request_id"])
        runtime: dict[str, Any] = {
            "protocol": "mycelium.full_context_recovery_runtime.v1",
            "binding": {
                "deployment_id": self._graph.deployment_id,
                "deployment_epoch": self._graph.deployment_epoch,
                "qualification_digest": qualification_digest,
                "a4_atomic_commit": atomic_commit,
            },
            "requests": requests,
            "breaker": {
                "state": "closed",
                "failure_observations_unix_ms": [],
                "open_until_unix_ms": 0,
            },
        }
        runtime["runtime_digest"] = self._digest_document(runtime)
        with self._lock:
            if self._recovery_qualification_digest not in (None, qualification_digest):
                raise M16AdmissionError("recovery_projection_binding_mismatch")
        return runtime

    def recovery_failure_reason(self, request_id: str) -> str | None:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is not None and pending.recovery_failure_reason is not None:
                return pending.recovery_failure_reason
            return self._recovery_failures.get(request_id)

    def cancel(self, request_id: str) -> bool:
        return self.cancel_with_deadline(
            request_id,
            deadline_monotonic_s=time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS,
        )

    def cancel_with_deadline(
        self,
        request_id: str,
        *,
        deadline_monotonic_s: float,
    ) -> bool:
        route_cancel = None
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None or pending.terminal_status is not None:
                return False
            coordinator = self._coordinator
            if coordinator is not None and coordinator.phase(request_id) == "queued":
                coordinator.cancel(request_id)
                self._request_command_cancellation(
                    pending,
                    deadline_monotonic_s=deadline_monotonic_s,
                )
                pending.cancellation_linearized = True
                pending.cancellation_requested.set()
                self._record_command_terminal(pending, "CANCELLED")
                pending.terminal_status = "CANCELLED"
                self._changed.notify_all()
                return True
            if self._recovery_awaits_successor_command(pending):
                # Between a source failure and its successor command no live
                # command exists to cancel: the failed one is already terminal.
                # The owner's cancellation linearizes on the router, and
                # recover_full_context refuses to fence the source or start a
                # successor behind it.
                pending.cancellation_linearized = True
                pending.cancellation_requested.set()
                self._changed.notify_all()
                return True
            cancellation_deadline = self._request_command_cancellation(
                pending,
                deadline_monotonic_s=deadline_monotonic_s,
            )
            # The command-controller CAS is terminal authority immediately,
            # including while physical cancel_request() is still returning.
            # Keep its state separate from the route's cancel_requested
            # callback: that callback must not become true until physical
            # fanout has installed the matching cancellation generation.
            pending.cancellation_linearized = True
            route_cancel = getattr(self._route, "cancel_request", None)
        if callable(route_cancel):
            try:
                route_cancel(
                    request_id,
                    deadline_monotonic_s=cancellation_deadline,
                )
            finally:
                with self._changed:
                    pending.cancellation_requested.set()
                    self._changed.notify_all()
        else:
            with self._changed:
                pending.cancellation_requested.set()
                self._changed.notify_all()
        return True

    def update_publisher_generation(
        self,
        request_id: str,
        *,
        expected_generation: int,
        new_generation: int,
    ) -> bool:
        """Advance gateway publication authority through the live command path."""

        route_update = None
        route_identity: dict[str, Any] | None = None
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None or pending.command_identity is None:
                return False
            identity = pending.command_identity
            if identity.publisher_generation == new_generation:
                return True
            if identity.publisher_generation != expected_generation:
                return False
            advanced = self._commands.advance_publisher_generation(
                identity,
                expected_generation=expected_generation,
                new_generation=new_generation,
            )
            if not advanced.accepted or advanced.snapshot is None:
                return False
            pending.command_identity = advanced.snapshot.identity
            if pending.execution_started:
                route_update = getattr(
                    self._route,
                    "update_publisher_generation",
                    None,
                )
                route_identity = dict(pending.route_identity or {})
        if callable(route_update):
            return bool(
                route_update(
                    request_id,
                    expected_generation=expected_generation,
                    new_generation=new_generation,
                    route_identity=route_identity,
                )
            )
        return True

    @staticmethod
    def _bounded_error_code(error: BaseException) -> str:
        code = getattr(error, "remote_code", None) or getattr(error, "code", None)
        if not isinstance(code, str) or not code:
            code = str(error).partition(":")[0]
        if (
            not code
            or len(code) > 64
            or not code[0].islower()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in code
            )
        ):
            return "runtime_error"
        return code

    @staticmethod
    def _digest_document(document: object) -> str:
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    document,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )

    @staticmethod
    def _command_identity_document(identity: CommandIdentity) -> dict[str, object]:
        return {
            "deployment_id": identity.deployment_id,
            "deployment_epoch": identity.deployment_epoch,
            "qualification_digest": identity.qualification_digest,
            "request_id": identity.request_id,
            "request_attempt": identity.request_attempt,
            "path_id": identity.path_id,
            "path_attempt": identity.path_attempt,
            "path_digest": identity.path_digest,
            "topology_generation": identity.topology_generation,
            "command_id": identity.command_id,
            "publisher_generation": identity.publisher_generation,
            "absolute_deadline_ms": identity.absolute_deadline_ms,
            "cancellation_generation": identity.cancellation_generation,
        }

    def _register_recovery_command(
        self,
        pending: _Pending,
        *,
        route_identity: Mapping[str, Any],
        placement_ids: tuple[str, ...],
        qualification_digest: str,
        publisher_generation: int,
        absolute_deadline_ms: int,
    ) -> None:
        if not placement_ids:
            raise M16AdmissionError("path_identity_invalid")
        graph_placement = next(
            (
                (stage, placement)
                for stage in self._graph.stages
                for placement in stage.placements
                if placement.placement_id == placement_ids[0]
            ),
            None,
        )
        if graph_placement is None:
            raise M16AdmissionError("path_identity_invalid")
        stage, placement = graph_placement
        issued_at_ms = int(time.monotonic() * 1_000)
        request_id = route_identity["request_id"]
        request_attempt = route_identity["request_attempt"]
        identity = CommandIdentity(
            deployment_id=route_identity["deployment_id"],
            deployment_epoch=route_identity["deployment_epoch"],
            qualification_digest=qualification_digest,
            request_id=request_id,
            request_attempt=request_attempt,
            path_id=route_identity["path_id"],
            path_attempt=route_identity["path_attempt"],
            path_digest=route_identity["path_manifest_digest"],
            topology_generation=route_identity["topology_generation"],
            command_id=f"request:{request_id}:attempt:{request_attempt}",
            publisher_generation=publisher_generation,
            absolute_deadline_ms=absolute_deadline_ms,
        )
        cleanup_owner_id = f"physical-live-route:{self._graph.deployment_id}"
        envelope = CommandEnvelope(
            identity=identity,
            stage_id=stage.stage_id,
            placement_id=placement.placement_id,
            assignment_id=placement.assignment_id,
            kind=CommandKind.PREFILL,
            issued_at_ms=issued_at_ms,
            idempotency_digest=self._digest_document(dict(route_identity)),
            cleanup_owner_id=cleanup_owner_id,
            maximum_request_bytes=131_072,
            maximum_response_bytes=16_777_216,
        )
        validate_interruptible_stage_command(
            {
                "protocol": "mycelium.interruptible_stage_command.v1",
                **self._command_identity_document(identity),
                "stage_id": envelope.stage_id,
                "placement_id": envelope.placement_id,
                "assignment_id": envelope.assignment_id,
                "command_kind": envelope.kind.value,
                "issued_at_ms": envelope.issued_at_ms,
                "idempotency_digest": envelope.idempotency_digest,
                "cooperative_step_ms": 100,
                "cleanup_owner_id": envelope.cleanup_owner_id,
                "maximum_request_bytes": envelope.maximum_request_bytes,
                "maximum_response_bytes": envelope.maximum_response_bytes,
                "expected_terminal_revision": envelope.expected_terminal_revision,
            }
        )
        try:
            with self._lock_order.scope("deployment", owner_id=request_id):
                registration = self._commands.register(envelope)
        except LockOrderViolation as error:
            raise M16AdmissionError(error.code) from error
        if not registration.accepted:
            raise M16AdmissionError(
                f"recovery_command_registration_failed:{registration.reason}"
            )
        pending.command_identity = identity
        pending.cleanup_owner_id = cleanup_owner_id
        pending.cleanup_receipt = None

    def _authorize_recovery_cleanup(self, pending: _Pending, deadline: float) -> float:
        """The successor route's cleanup authorization, recorded for A6 seams.

        The route asks for it once generation has ended (at EOS or at the
        token bound), so it marks the successor's output as finished. An A6
        hold armed by the fault control delays the authorization (bounded)
        until the fault seam releases it; unarmed it never waits.
        """

        hold = None
        with self._changed:
            if (not pending.cancellation_requested.is_set()
                    and not pending.cancellation_linearized):
                pending.recovery_output_finished = True
                hold = pending.a6_completion_cleanup_hold
                if hold is not None and not hold.is_set():
                    pending.a6_completion_cleanup_held = True
                else:
                    hold = None
            self._changed.notify_all()
        if hold is not None:
            hold.wait(timeout=_A6_COMPLETION_CLEANUP_HOLD_SECONDS)
            with self._changed:
                pending.a6_completion_cleanup_held = False
                self._changed.notify_all()
        return self._request_command_cancellation(
            pending,
            deadline_monotonic_s=deadline,
            completion_cleanup=True,
        )

    def _recovery_awaits_successor_command(self, pending: _Pending) -> bool:
        """A recovery is pending and no nonterminal command exists for it yet."""

        if not pending.recovery_required:
            return False
        identity = pending.command_identity
        if identity is None:
            return True
        snapshots = self._commands.snapshot(
            identity.request_id,
            request_attempt=identity.request_attempt,
        )
        return not snapshots or any(
            snapshot.terminal is not None for snapshot in snapshots
        )

    def _request_command_cancellation(
        self,
        pending: _Pending,
        *,
        deadline_monotonic_s: float | None = None,
        completion_cleanup: bool = False,
    ) -> float:
        identity = pending.command_identity
        if identity is None:
            return time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
        if identity.cancellation_generation > 0:
            if not completion_cleanup:
                promoted = self._commands.promote_completion_cleanup_to_cancellation(
                    identity,
                    observed_at_ms=int(time.monotonic() * 1_000),
                )
                if not promoted.accepted:
                    raise RuntimeError(
                        f"command_cancellation_rejected:{promoted.reason}"
                    )
            snapshots = self._commands.snapshot(
                identity.request_id,
                request_attempt=identity.request_attempt,
            )
            if len(snapshots) != 1 or snapshots[0].cleanup_deadline_ms is None:
                raise RuntimeError("command_cancellation_deadline_missing")
            return snapshots[0].cleanup_deadline_ms / 1_000.0
        now_ms = int(time.monotonic() * 1_000)
        cleanup_deadline_ms = (
            None if deadline_monotonic_s is None else int(deadline_monotonic_s * 1_000)
        )
        result = self._commands.cancel(
            identity,
            new_cancellation_generation=identity.cancellation_generation + 1,
            observed_at_ms=now_ms,
            idempotency_digest=self._digest_document(
                {
                    "command_id": identity.command_id,
                    "cancellation_generation": identity.cancellation_generation + 1,
                }
            ),
            cleanup_deadline_ms=cleanup_deadline_ms,
            completion_cleanup=completion_cleanup,
        )
        if not result.accepted or result.snapshot is None:
            raise RuntimeError(f"command_cancellation_rejected:{result.reason}")
        pending.command_identity = result.snapshot.identity
        if result.snapshot.cleanup_deadline_ms is None:
            raise RuntimeError("command_cancellation_deadline_missing")
        return result.snapshot.cleanup_deadline_ms / 1_000.0

    def _publish_route_cleanup_receipt(
        self,
        pending: _Pending,
        receipt: Mapping[str, Any],
        *,
        expected_identity: CommandIdentity | None = None,
    ) -> None:
        """Publish physical cleanup at the route proof boundary."""

        should_release = False
        late_finalized = False
        with self._changed:
            if expected_identity is not None:
                current = pending.command_identity
                if current is None or (
                    current.request_attempt != expected_identity.request_attempt
                    or current.path_id != expected_identity.path_id
                    or current.path_attempt != expected_identity.path_attempt
                    or current.command_id != expected_identity.command_id
                ):
                    # An old callback cannot replace the successor receipt.
                    return
            # A route-owner proof can arrive after the worker blocked on a
            # missing receipt and the gateway already published failure-to-prove.
            # Only the still-owned, exact current attempt may finish retained
            # reservations. This is a NEW command terminal CAS (not a v2 zero-
            # CAS observation); the gateway's earlier notice is never rewritten.
            coordinator = self._coordinator
            request = pending.request
            request_id = None if request is None else request.request_id
            if (
                expected_identity is not None
                and coordinator is not None
                and request_id is not None
                and self._pending.get(request_id) is pending
                and pending.terminal_blocked_reason is not None
                and pending.terminal_status is None
                and not pending.recovery_required
                and not pending.cancellation_linearized
            ):
                if (
                    pending.route_identity is None
                    or coordinator.route_identity(request_id) != pending.route_identity
                    or coordinator.phase(request_id) != "cleanup"
                    or coordinator.terminal_state(request_id) is not None
                ):
                    raise RuntimeError("command_cleanup_authority_changed")
                # Validate and commit against the actual route receipt before
                # touching the coordinator. A foreign or incomplete receipt
                # cannot release a reservation or poison the retained proof.
                self._record_command_terminal(pending, "FAILED", cleanup_receipt=receipt)
                coordinator.complete(request_id, state="failed")
                pending.cleanup_receipt = dict(receipt)
                pending.terminal_blocked_reason = None
                pending.terminal_status = "FAILED"
                late_finalized = True
                should_release = pending.release_requested
                if should_release:
                    self._pending.pop(request_id, None)
                    self._sinks.pop(request_id, None)
                    self._retain_terminal_status(request_id, pending.terminal_status, pending)
                self._changed.notify_all()
            else:
                # Retain the exact receipt with command ownership before any
                # terminal publication. A racing worker must not fall back to
                # a route lookup after its request-scoped copy is released.
                pending.cleanup_receipt = dict(receipt)
            if pending.cancellation_linearized:
                self._record_command_terminal(
                    pending,
                    "CANCELLED",
                    cleanup_receipt=receipt,
                )
                # The route worker can return in the narrow interval after
                # physical teardown proves clean but before cancel_request()
                # publishes this receipt.  It then fail-closes terminal
                # publication and retires only the dispatch slot.  The exact
                # owner receipt is authoritative recovery for that ordering:
                # finish retained M16 reservations and revive the pending
                # terminal instead of leaving a proven-clean request stuck in
                # cleanup forever.
                coordinator = self._coordinator
                request = pending.request
                request_id = None if request is None else request.request_id
                phase = (
                    None
                    if coordinator is None or request_id is None
                    else coordinator.phase(request_id)
                )
                if coordinator is not None and request_id is not None:
                    if phase == "cleanup":
                        coordinator.complete(request_id, state="cancelled")
                    elif phase not in {
                        "queued",
                        "prefill",
                        "first_token",
                        "decode",
                        "cancelled",
                    }:
                        raise RuntimeError("command_cleanup_phase_invalid")
                if phase in {"cleanup", "cancelled"} or coordinator is None:
                    pending.terminal_error_code = None
                    pending.terminal_blocked_reason = None
                    pending.terminal_status = "CANCELLED"
                    if pending.release_requested and request_id is not None:
                        self._pending.pop(request_id, None)
                        self._sinks.pop(request_id, None)
                        self._retain_terminal_status(request_id, pending.terminal_status, pending)
                        should_release = True
                    self._changed.notify_all()
            elif not late_finalized:
                self._record_command_cleanup(pending, receipt=receipt)
        if should_release:
            self._release_route_request(pending)
            self._retire_command(pending)

    def _dead_peer_cleanup_receipt(
        self,
        identity: CommandIdentity,
        owner_id: str,
    ) -> dict[str, Any] | None:
        """Seal exact cleanup when the only unproven participants have exited.

        The physical route seals a request cleanup receipt only after every
        participating node returned an exact clean receipt. A peer whose process
        was killed can never return one, so a killed source would strand its
        request in cleanup and make recovery unreachable. Process exit releases
        every resource that process held, so the route's own identity-bound
        ``node_process_exited`` incident, confirmed by the peer process's exit status,
        is that peer's cleanup proof. Every surviving participant must still
        prove exact cleanup itself. A peer that is only disconnected or stalled
        (process alive) proves nothing, and the request stays terminal-blocked.
        """

        request_id = identity.request_id
        route = _route_that_ran(self._route, request_id)
        # Fields fixed for the command's lifetime. Cancellation generation is
        # not among them: the failure itself joins the command to the route's
        # next generation, and the incident keeps the one it was observed at.
        fixed = {
            "deployment_id": identity.deployment_id,
            "deployment_epoch": identity.deployment_epoch,
            "qualification_digest": identity.qualification_digest,
            "request_attempt": identity.request_attempt,
            "path_id": identity.path_id,
            "path_attempt": identity.path_attempt,
            "path_digest": identity.path_digest,
            "topology_generation": identity.topology_generation,
            "command_id": identity.command_id,
        }

        def generation_at_most(value: object, current: int) -> bool:
            return type(value) is int and 0 <= value <= current

        try:
            incidents = route.a4_scoped_runtime_incidents()
            active = dict(route._active_route_requests.get(request_id) or {})
            sessions = dict(route._sessions)
            subject = route._request_cleanup_subject(request_id)
            cleanup_complete = route._cancellation_cleanup_complete
            store_receipt = route._store_request_cleanup_receipt
        except Exception:
            return None
        participating = active.get("participating_node_ids")
        if not isinstance(participating, (set, frozenset)) or not participating:
            return None
        # The cleanup subject is the exact generation the route cancelled; it
        # must be the command's current generation, as for a node receipt.
        if (
            any(active.get(field) != value for field, value in fixed.items())
            or subject.get("request_id") != request_id
            or any(subject.get(field) != value for field, value in fixed.items())
            or subject.get("cancellation_generation") != identity.cancellation_generation
            or not 1 <= (subject.get("publisher_generation") or 0) <= identity.publisher_generation
        ):
            return None
        exited = frozenset(
            incident.get("subject_id")
            for incident in incidents
            if isinstance(incident, Mapping)
            and incident.get("request_id") == request_id
            and incident.get("scope") == "request"
            and incident.get("reason") == "node_process_exited"
            and incident.get("fatal_accepted") is False
            and incident.get("cleanup_owner_id") == owner_id
            and all(incident.get(field) == value for field, value in fixed.items())
            and generation_at_most(
                incident.get("cancellation_generation"), identity.cancellation_generation
            )
            and generation_at_most(
                incident.get("publisher_generation"), identity.publisher_generation
            )
        )
        if not exited or not exited <= participating:
            return None
        for node_id in exited:
            returncode = getattr(sessions.get(node_id), "returncode", None)
            if type(returncode) is not int:
                return None
        survivors = frozenset(participating) - exited
        try:
            if survivors and cleanup_complete(survivors, cleanup_subject=subject) is not True:
                return None
            receipt = store_receipt(
                request_id=request_id,
                cleanup_subject=subject,
                participating_node_ids=frozenset(participating),
                deployment_id=identity.deployment_id,
            )
        except Exception:
            return None
        if not isinstance(receipt, Mapping) or receipt.get("cleanup_owner_id") != owner_id:
            return None
        _LOGGER.warning(
            "request cleanup sealed with exited peers request_id=%s exited=%s survivors=%s",
            request_id,
            sorted(exited),
            sorted(survivors),
        )
        return dict(receipt)

    def _record_command_cleanup(
        self,
        pending: _Pending,
        *,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        identity = pending.command_identity
        owner_id = pending.cleanup_owner_id
        if identity is None or owner_id is None:
            return
        if receipt is None and pending.cleanup_receipt is not None:
            receipt = dict(pending.cleanup_receipt)
        if receipt is None:
            scoped_receipt_source = getattr(
                _route_that_ran(self._route, identity.request_id),
                "request_cleanup_receipt_scoped",
                None,
            )
            if callable(scoped_receipt_source):
                receipt = scoped_receipt_source(
                    identity.request_id,
                    request_attempt=identity.request_attempt,
                    path_id=identity.path_id,
                    path_attempt=identity.path_attempt,
                    path_digest=identity.path_digest,
                    cleanup_owner_id=owner_id,
                )
            else:
                receipt_source = getattr(self._route, "request_cleanup_receipt", None)
                receipt = (
                    receipt_source(identity.request_id)
                    if callable(receipt_source)
                    else None
                )
        if (
            receipt is None
            and pending.execution_started
            and getattr(self._route, "is_simulated", False) is not True
        ):
            receipt = self._dead_peer_cleanup_receipt(identity, owner_id)
        if receipt is None:
            if (
                pending.execution_started
                and getattr(self._route, "is_simulated", False) is not True
            ):
                raise RuntimeError("command_cleanup_receipt_missing")
            receipt = {
                "deployment_id": identity.deployment_id,
                "deployment_epoch": identity.deployment_epoch,
                "qualification_digest": identity.qualification_digest,
                "request_id": identity.request_id,
                "request_attempt": identity.request_attempt,
                "path_id": identity.path_id,
                "path_attempt": identity.path_attempt,
                "path_digest": identity.path_digest,
                "topology_generation": identity.topology_generation,
                "command_id": identity.command_id,
                "cancellation_generation": identity.cancellation_generation,
                "publisher_generation": identity.publisher_generation,
                "cleanup_owner_id": owner_id,
                "node_ids": [],
                "simulation_only": bool(pending.execution_started),
            }
        # Physical cleanup is fenced by the immutable request/path/command and
        # cancellation generation that was actually torn down.  Publisher
        # generation is independent gateway replay authority: it may advance
        # after the route freezes its cleanup subject but before the receipt
        # callback reaches this adapter.  The command controller has already
        # CAS-validated every such advance, so an older positive publisher
        # generation remains exact cleanup proof for the current command.  A
        # future generation is impossible and still fails closed.
        identity_fields = (
            ("deployment_id", identity.deployment_id),
            ("deployment_epoch", identity.deployment_epoch),
            ("qualification_digest", identity.qualification_digest),
            ("request_id", identity.request_id),
            ("request_attempt", identity.request_attempt),
            ("path_id", identity.path_id),
            ("path_attempt", identity.path_attempt),
            ("path_digest", identity.path_digest),
            ("topology_generation", identity.topology_generation),
            ("command_id", identity.command_id),
            (
                "cancellation_generation",
                identity.cancellation_generation,
            ),
        )
        identity_mismatch_fields = tuple(
            field
            for field, expected in identity_fields
            if receipt.get(field) != expected
        )
        receipt_publisher_generation = receipt.get("publisher_generation")
        publisher_generation_mismatch = (
            type(receipt_publisher_generation) is not int
            or not 1
            <= receipt_publisher_generation
            <= identity.publisher_generation
        )
        if (
            identity_mismatch_fields
            or publisher_generation_mismatch
        ):
            _LOGGER.warning("%s", json.dumps({
                "event": "a6_cleanup_receipt_identity_mismatch",
                "request_id": identity.request_id,
                "request_attempt": identity.request_attempt,
                "fields": list(identity_mismatch_fields),
                "receipt_cancellation_generation": receipt.get("cancellation_generation"),
                "command_cancellation_generation": identity.cancellation_generation,
                "receipt_publisher_generation": receipt_publisher_generation,
                "command_publisher_generation": identity.publisher_generation,
                "recovery_phase": pending.recovery_phase,
                "completion_cleanup_held": pending.a6_completion_cleanup_held,
            }, sort_keys=True, separators=(",", ":"), default=str))
            _LOGGER.error(
                "cleanup receipt identity mismatch request_id=%s fields=%s "
                "receipt_publisher_generation=%r controller_publisher_generation=%r "
                "receipt_cancellation_generation=%r "
                "controller_cancellation_generation=%r",
                identity.request_id,
                identity_mismatch_fields,
                receipt_publisher_generation,
                identity.publisher_generation,
                receipt.get("cancellation_generation"),
                identity.cancellation_generation,
            )
            raise RuntimeError("command_cleanup_receipt_identity_mismatch")
        if receipt.get("cleanup_owner_id") != owner_id:
            raise RuntimeError("command_cleanup_receipt_owner_mismatch")
        node_ids = receipt.get("node_ids", [])
        if not isinstance(node_ids, list) or not all(
            isinstance(node_id, str) and node_id for node_id in node_ids
        ):
            raise RuntimeError("command_cleanup_receipt_invalid")
        completed_at_ms = receipt.get("completed_at_monotonic_ms")
        now_ms = (
            completed_at_ms
            if isinstance(completed_at_ms, int)
            and not isinstance(completed_at_ms, bool)
            else int(time.monotonic() * 1_000)
        )
        cleanup = self._commands.record_cleanup(
            identity,
            owner_id=owner_id,
            result=CleanupResult(
                status=CleanupStatus.COMPLETED,
                released_resource_count=len(node_ids),
                result_digest=self._digest_document(receipt),
            ),
            observed_at_ms=now_ms,
            expected_cleanup_revision=0,
        )
        if not cleanup.accepted:
            raise RuntimeError(f"command_cleanup_rejected:{cleanup.reason}")

    def _record_command_terminal(
        self,
        pending: _Pending,
        terminal: str,
        *,
        cleanup_receipt: Mapping[str, Any] | None = None,
    ) -> str:
        identity = pending.command_identity
        owner_id = pending.cleanup_owner_id
        if identity is None or owner_id is None:
            return terminal
        if terminal == "CANCELLED":
            if identity.cancellation_generation == 0:
                self._request_command_cancellation(pending)
                identity = pending.command_identity
                assert identity is not None
            status = TerminalStatus.CANCELLED
        elif terminal == "COMPLETED":
            status = TerminalStatus.COMPLETED
        else:
            status = TerminalStatus.ERROR
        self._record_command_cleanup(pending, receipt=cleanup_receipt)
        completed_at_ms = (
            cleanup_receipt.get("completed_at_monotonic_ms")
            if cleanup_receipt is not None
            else None
        )
        now_ms = (
            completed_at_ms
            if isinstance(completed_at_ms, int)
            and not isinstance(completed_at_ms, bool)
            else int(time.monotonic() * 1_000)
        )
        result = TerminalResult(
            identity=identity,
            status=status,
            observed_at_ms=now_ms,
            result_digest=self._digest_document(
                {"request_id": identity.request_id, "terminal": terminal}
            ),
            error_code="runtime_error" if status is TerminalStatus.ERROR else None,
        )
        mutation = self._commands.terminal_compare_and_swap(
            result,
            expected_terminal_revision=0,
        )
        if not mutation.accepted:
            snapshot = mutation.snapshot
            if (
                mutation.reason == "already_terminal"
                and snapshot is not None
                and snapshot.identity == identity
                and snapshot.terminal is not None
                and snapshot.terminal.status is status
                and snapshot.cleanup_result is not None
                and snapshot.cleanup_result.status is CleanupStatus.COMPLETED
            ):
                # The controller CAS is the terminal linearization point.  A
                # watchdog/cancellation observer can re-enter this adapter
                # after that CAS but before _Pending.terminal_status is
                # published. Exact identity, terminal status, and cleanup
                # proof make that observation idempotent; every conflicting
                # terminal continues to fail closed below.
                return terminal
            if (
                mutation.reason == "already_terminal"
                and snapshot is not None
                and snapshot.identity == identity
                and snapshot.terminal is not None
                and snapshot.terminal.status is TerminalStatus.DEADLINE_EXCEEDED
                and snapshot.cleanup_result is not None
                and snapshot.cleanup_result.status is CleanupStatus.COMPLETED
            ):
                # Cleanup proof and terminal CAS are deliberately separate.
                # If proof completes at the fixed cleanup deadline, the
                # controller can install DEADLINE_EXCEEDED between those two
                # operations. Preserve that authoritative terminal and project
                # it as a bounded failed request instead of stranding an exact,
                # fully cleaned command in the runtime cleanup phase.
                return "FAILED"
            raise RuntimeError(f"command_terminal_rejected:{mutation.reason}")
        return terminal

    def runtime_status(self) -> dict[str, Any] | None:
        coordinator = self._coordinator
        return None if coordinator is None else coordinator.status()

    def release_request(self, request_id: str) -> None:
        should_release = False
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None:
                return
            if pending.terminal_status is None:
                pending.release_requested = True
            else:
                self._pending.pop(request_id, None)
                self._sinks.pop(request_id, None)
                self._retain_terminal_status(request_id, pending.terminal_status, pending)
                should_release = True
            self._changed.notify_all()
        if should_release:
            self._release_route_request(pending)
            self._retire_command(pending)

    def _release_predecessor_attempt(
        self, pending: _Pending, predecessor: CommandIdentity
    ) -> None:
        """Retire the failed attempt's route state before its successor runs.

        A physical route keeps one active record and one cleanup receipt per
        request id. The failed attempt's exact cleanup receipt (sealed when
        its command terminal was recorded) would otherwise collide with the
        successor's own receipt on the same route, and the successor's exact
        cleanup could never be stored. Release only the predecessor's exact
        scoped identity, after its cleanup proof; never a request-wide release
        (that would let a route owner replace the route under the successor).
        """

        owner_id = pending.cleanup_owner_id
        release = getattr(self._route, "release_attempt_scoped", None)
        if not callable(release):
            release = getattr(self._route, "release_request_scoped", None)
        if owner_id is None or not callable(release):
            return
        if getattr(self._route, "is_simulated", False) is True:
            return
        release(
            predecessor.request_id,
            request_attempt=predecessor.request_attempt,
            path_id=predecessor.path_id,
            path_attempt=predecessor.path_attempt,
            path_digest=predecessor.path_digest,
            cleanup_owner_id=owner_id,
        )

    def _release_route_request(self, pending: _Pending) -> None:
        identity = pending.command_identity
        owner_id = pending.cleanup_owner_id
        scoped_release = getattr(self._route, "release_request_scoped", None)
        if (
            pending.execution_started
            and identity is not None
            and owner_id is not None
            and callable(scoped_release)
        ):
            scoped_release(
                identity.request_id,
                request_attempt=identity.request_attempt,
                path_id=identity.path_id,
                path_attempt=identity.path_attempt,
                path_digest=identity.path_digest,
                cleanup_owner_id=owner_id,
            )
            return
        request = pending.request
        if request is not None:
            self._route.release_request(request.request_id)

    def _retire_command(self, pending: _Pending) -> None:
        identity = pending.command_identity
        if identity is None:
            return
        # Keep the exact command counts past retirement for the A6 cleanup
        # observation; bounded, and never a substitute for an unread count.
        try:
            receipt = self._command_authority_snapshot(identity)
        except Exception:
            receipt = None
        if receipt is not None:
            with self._lock:
                retained = getattr(self, "_a6_retired_command_authority", None)
                if retained is None:
                    retained = {}
                    self._a6_retired_command_authority = retained
                retained.pop(identity.request_id, None)
                retained[identity.request_id] = receipt
                while len(retained) > 256:
                    retained.pop(next(iter(retained)))
        retired = self._commands.retire(
            identity.request_id,
            expected_attempt=identity.request_attempt,
        )
        if not retired.accepted and retired.reason != "request_unknown":
            raise RuntimeError(f"command_retirement_rejected:{retired.reason}")

    def close(self, *, timeout_seconds: float = ROUTER_CLOSE_TIMEOUT_SECONDS) -> None:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("invalid_shutdown_timeout")
        shutdown_deadline = time.monotonic() + float(timeout_seconds)
        with self._changed:
            if self._closed:
                return
            self._closed = True
            request_ids = tuple(
                request_id
                for request_id, pending in self._pending.items()
                if pending.terminal_status is None
            )
            self._changed.notify_all()
        cancellation_errors: list[BaseException] = []

        def cancel_owned_request(request_id: str) -> None:
            try:
                self.cancel(request_id)
            except BaseException as error:
                cancellation_errors.append(error)

        cancellation_threads = tuple(
            threading.Thread(
                target=cancel_owned_request,
                args=(request_id,),
                name=f"live-route-shutdown-cancel-{index}",
                daemon=True,
            )
            for index, request_id in enumerate(request_ids)
        )
        for thread in cancellation_threads:
            thread.start()
        for thread in cancellation_threads:
            thread.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
        if any(thread.is_alive() for thread in cancellation_threads):
            raise RuntimeError("router_port_shutdown_cancellation_timeout")
        dispatcher = self._dispatcher
        if dispatcher is not None and dispatcher is not threading.current_thread():
            dispatcher.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
            if dispatcher.is_alive():
                raise RuntimeError("router_port_dispatcher_shutdown_timeout")
        self._worker_pool.shutdown(wait=False, cancel_futures=True)
        worker_threads = tuple(getattr(self._worker_pool, "_threads", ()))
        for thread in worker_threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
        if any(thread.is_alive() for thread in worker_threads):
            raise RuntimeError("router_port_worker_shutdown_timeout")
        if cancellation_errors:
            raise RuntimeError("router_port_shutdown_cancellation_failed") from (
                cancellation_errors[0]
            )
        with self._changed:
            releasable = tuple(
                (request_id, pending)
                for request_id, pending in self._pending.items()
                if pending.terminal_status is not None
            )
            blocked_request_ids = tuple(
                request_id
                for request_id, pending in self._pending.items()
                if pending.terminal_status is None
            )
            for request_id, _pending in releasable:
                self._pending.pop(request_id, None)
                self._sinks.pop(request_id, None)
                self._retain_terminal_status(request_id, _pending.terminal_status, _pending)
        for request_id, pending in releasable:
            self._release_route_request(pending)
            self._retire_command(pending)
        if blocked_request_ids:
            raise RuntimeError("router_port_shutdown_cleanup_unproven")


__all__ = ["LiveRouterPort"]
