# SPDX-License-Identifier: AGPL-3.0-only
"""Bind the A6 controlled fault to real peer and path failures.

Every case in ``mycelium_a6_fault_control.INJECTION_POINTS`` starts from the
same measured precondition: one request admitted through the ordinary gateway
and executing on its source attempt. The shared trigger is a real failure of
the request's assigned source peer, killed with ``SIGKILL``. For a node the
serve process launched locally the product signals the owned process group of
that node's control session. For a node reached over ssh it never signals the
local ssh client: it runs a bounded script on the remote host, through the
peer's own pinned ssh argv, that re-verifies the worker PID the node reported
in its authenticated hello (its command line must name
``physical_inference_node.py`` and the exact ``--run-id``) and signals that
worker and its children (for example its sidecar) there. Either way the route
observes the failure through its own reader and records an A4 scoped runtime
incident.

Case-specific faults then use only real product levers:

* peer stalls (``SIGSTOP``/``SIGCONT``) and further kills of the reserved
  successor's nodes (prefill failure, timeout, exhausted attempts, cancellation
  during prefill),
* an owner cancellation through the gateway,
* a grant-gated one-shot qualification fault in the live qualifier that makes
  it *issue* a record the recovery authority must refuse,
* withholding every successor candidate for the one owned request,
* a late publication attempt with the fenced source identity,
* a participant loss after the last token so cleanup cannot be proven, and
* the product's own shutdown.

Nothing here reports what a case expects. Each seam returns the facts it
measured from the router, route, gateway, coordinator and qualifier after the
fault; ``ControlledFaultMechanism`` alone compares them with the case contract
and refuses the case when they differ.
"""
from __future__ import annotations

import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from mycelium_a6_fault_control import EXACT_FLEET_GRANT, FaultRejected, INJECTION_POINTS
from physical_inference_qualification import ControllerError, _peer_process_argv

IDENTITY_FIELDS = (
    "deployment_id", "deployment_epoch", "qualification_digest", "request_id",
    "request_attempt", "path_id", "path_attempt", "path_digest",
    "topology_generation", "command_id", "publisher_generation", "cancellation_generation",
)
QUALIFICATION_CASES = {
    "incompatible_successor": ("incompatible", "recovery_compatibility_mismatch"),
    "stale_qualification": ("stale", "recovery_qualification_stale"),
    "foreign_qualification": ("foreign", "recovery_request_binding_mismatch"),
    "unexecuted_qualification": ("unexecuted", "recovery_physical_qualification_required"),
    "mismatched_path_attempt": ("mismatched_path_attempt", "recovery_successor_binding_mismatch"),
}
_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
_FENCED_PHASES = frozenset({"recovery_prefill", "cutover_pending", "recovered_decode"})
# Recovery phases in which no successor has fenced the source or started work.
_PRE_RECOVERY_PHASES = frozenset({None, "active", "failure_observed", "successor_pending"})

#: Signals a remote fault may deliver, by the name the remote script accepts.
_REMOTE_SIGNAL_NAMES = {
    signal.SIGKILL: "KILL",
    signal.SIGSTOP: "STOP",
    signal.SIGCONT: "CONT",
}
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
REMOTE_SIGNAL_TIMEOUT_SECONDS = 30.0
#: Bound of each timeout-case wait. A stalled successor is interrupted at the
#: router's prefill (default 30 s) or progress (default 15 s) recovery
#: deadline, see ``RecoveryPhaseDeadlines``.
RECOVERY_DEADLINE_WAIT_SECONDS = 120.0
#: Exit status of the remote script when the PID is not the bound node worker.
REMOTE_IDENTITY_MISMATCH_EXIT = 65
#: Runs on the peer's host as ``/bin/sh -c SCRIPT a6-fault-signal SIG PID RUN_ID``.
#: It re-verifies that PID is this run's node worker before signalling, and
#: signals only that worker's process group (when the worker leads it) or the
#: worker's direct children and then the worker. It never touches the ssh
#: session that carried it to the host.
REMOTE_SIGNAL_SCRIPT = r"""set -u
sig=$1
pid=$2
run_id=$3
case $sig in KILL|STOP|CONT) ;; *) exit 64 ;; esac
case $pid in ''|*[!0-9]*) exit 64 ;; esac
case $run_id in ''|*[!A-Za-z0-9._:-]*) exit 64 ;; esac
[ "$pid" -gt 1 ] || exit 64
args=$(ps -ww -o args= -p "$pid" 2>/dev/null) || exit 65
case " $args " in *" "*physical_inference_node.py" "*) ;; *) exit 65 ;; esac
case " $args " in *" --run-id $run_id "*) ;; *) exit 65 ;; esac
pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ') || exit 65
if [ "$pgid" = "$pid" ]; then
  kill -s "$sig" -- "-$pid" || exit 66
else
  command -v pkill >/dev/null 2>&1 || exit 67
  pkill "-$sig" -P "$pid"
  [ $? -le 1 ] || exit 66
  kill -s "$sig" "$pid" || exit 66
fi
"""


def remote_signal_command(signum: int, process_id: int, run_id: str) -> tuple[str, ...]:
    """The remote argv that signals one verified node worker on its own host."""

    name = _REMOTE_SIGNAL_NAMES.get(signum)
    if name is None:
        raise FaultRejected("a6_fault_remote_signal_unsupported")
    if type(process_id) is not int or process_id <= 1:
        raise FaultRejected("a6_fault_remote_process_identity_unavailable")
    if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise FaultRejected("a6_fault_remote_run_identity_unavailable")
    return ("/bin/sh", "-c", REMOTE_SIGNAL_SCRIPT, "a6-fault-signal",
            name, str(process_id), run_id)


def _run_remote(argv: tuple[str, ...]) -> int:
    try:
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=REMOTE_SIGNAL_TIMEOUT_SECONDS,
            check=False).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


@dataclass
class ExecutedCase:
    """One executed product fault, retained for the source-owned observation."""

    case_id: str
    request_id: str
    binding: dict[str, Any]
    source_node_id: str
    armed_reservation_count: int | None
    deadline_unix_ms: int | None = None
    post_state: dict[str, Any] = field(default_factory=dict)
    #: (gateway event sequence, gateway-committed count) when the fault landed.
    failure_marker: tuple[int, int] | None = None
    #: The concrete route that executed the source attempt. Its own A4 reader
    #: records the source incident; a cold restart after the request is
    #: released replaces the owner's current route, so the incident is read
    #: from this route, never from its replacement.
    source_route: Any = field(default=None, repr=False, compare=False)

    @property
    def committed_at_failure(self) -> int | None:
        return None if self.failure_marker is None else self.failure_marker[1]


def _incident_identity_matches(incident: Mapping[str, Any],
                               identity: Mapping[str, Any]) -> bool:
    """The incident names the armed command; its generation may have advanced.

    The route records the incident at whichever cancellation generation is
    current when its reader observes the failure: before or after the failure
    joins the command to the next generation. It never precedes the armed one.
    """

    for name, value in identity.items():
        if name not in incident:
            continue
        observed = incident[name]
        if name == "cancellation_generation":
            if type(observed) is not int or type(value) is not int or observed < value:
                return False
        elif observed != value:
            return False
    return True


class LiveProductFaultTarget:
    """Real product authorities one controlled fault reads and acts on."""

    def __init__(
        self,
        *,
        router: Any,
        route: Any,
        service: Any,
        qualifier: Any | None = None,
        request_stop: Callable[[], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        grant: str = EXACT_FLEET_GRANT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wait_seconds: float = 30.0,
        poll_seconds: float = 0.01,
        remote_runner: Callable[[tuple[str, ...]], int] = _run_remote,
    ) -> None:
        if grant != EXACT_FLEET_GRANT:
            raise FaultRejected("explicit_fleet_grant_required")
        self.router = router
        self.route = route
        self.service = service
        self.qualifier = qualifier
        self._request_stop = request_stop
        self._stop_requested = stop_requested
        self._grant = grant
        self._clock = clock
        self._sleep = sleep
        self._wait_seconds = wait_seconds
        self._poll = poll_seconds
        self._lock = threading.Lock()
        self._armed: dict[str, ExecutedCase] = {}
        self._executed: list[ExecutedCase] = []
        #: Stalled node -> the route whose node was stalled, so it is resumed
        #: on exactly the process(es) that were stopped.
        self._stopped_nodes: dict[str, Any] = {}
        self._remote_runner = remote_runner
        self._listeners: list[Callable[[ExecutedCase], None]] = []

    def add_listener(self, listener: Callable[[ExecutedCase], None]) -> None:
        """Be told about every executed case (for source-owned observations)."""
        with self._lock:
            self._listeners.append(listener)

    # -- reads ---------------------------------------------------------------

    def _concrete_route(self) -> Any:
        current = getattr(self.route, "_current_route_or_raise", None)
        return current() if callable(current) else self.route

    def view(self, request_id: str) -> dict[str, Any]:
        view = self.router.a6_request_view(request_id)
        return {} if view is None else dict(view)

    def incidents(self, route: Any | None = None) -> list[dict[str, Any]]:
        source = self._concrete_route() if route is None else route
        reader = getattr(source, "a4_scoped_runtime_incidents", None)
        return [] if not callable(reader) else [dict(item) for item in reader()]

    def gateway_terminal_count(self, request_id: str) -> int | None:
        try:
            return int(self.service.terminal_event_count(request_id))
        except Exception:
            return None

    def reservation_count(self, request_id: str) -> int | None:
        coordinator = getattr(self.router, "_coordinator", None)
        if coordinator is None:
            return None
        for item in coordinator.status()["requests"]:
            if item["request_id"] == request_id:
                return int(item["reservation_count"])
        return None

    def coordinator_terminal(self, request_id: str) -> str | None:
        coordinator = getattr(self.router, "_coordinator", None)
        return None if coordinator is None else coordinator.terminal_state(request_id)

    def committed_route_identity(self, request_id: str) -> dict[str, Any] | None:
        """The coordinator's committed route identity for one request.

        The router's retained view swaps its route identity to the successor
        only at cutover, and a completed request is released immediately
        after; the coordinator's record survives release and is updated by
        the cutover compare-and-swap itself, so it is the authority.
        """

        coordinator = getattr(self.router, "_coordinator", None)
        reader = getattr(coordinator, "route_identity", None)
        if not callable(reader):
            return None
        try:
            identity = reader(request_id)
        except Exception:
            return None
        return dict(identity) if isinstance(identity, Mapping) else None

    def gateway_committed(self, request_id: str) -> tuple[int, int] | None:
        from mycelium_a6_case_observations import gateway_failure_marker
        return gateway_failure_marker(self.service, request_id)

    def placement_nodes(self) -> dict[str, str]:
        return dict(self.router.a6_placement_node_ids())

    @property
    def wait_seconds(self) -> float:
        return self._wait_seconds

    def wait(self, predicate: Callable[[], Any], seconds: float | None = None,
             poll_seconds: float | None = None) -> Any:
        deadline = self._clock() + (self._wait_seconds if seconds is None else seconds)
        while True:
            value = predicate()
            if value:
                return value
            if self._clock() >= deadline:
                return None
            self._sleep(self._poll if poll_seconds is None else poll_seconds)

    def gateway_max_tokens(self, request_id: str) -> int | None:
        try:
            return int(self.service._get_session(request_id).max_new_tokens)
        except Exception:
            return None

    # -- the shared admitted precondition -------------------------------------

    def arm(self, case_id: str) -> dict[str, Any]:
        """Bind the one admitted in-flight request; refuse any ambiguity."""

        inflight = self.router.a6_inflight_request_ids()
        if len(inflight) != 1:
            raise FaultRejected("a6_fault_inflight_request_unavailable")
        request_id = inflight[0]
        if self.gateway_terminal_count(request_id) != 0:
            raise FaultRejected("a6_fault_gateway_terminal_or_unavailable")
        binding = self.router.a6_controlled_fault_binding(request_id)
        if binding is None or binding.get("request_id") != request_id:
            raise FaultRejected("a6_fault_identity_unavailable")
        source = binding.get("source_node_id")
        if not isinstance(source, str) or not source:
            raise FaultRejected("a6_fault_source_unavailable")
        try:
            source_route = self._concrete_route()
        except Exception as error:
            raise FaultRejected("a6_fault_route_unavailable") from error
        with self._lock:
            prior = self._armed.get(case_id)
            if prior is not None and prior.binding != binding:
                raise FaultRejected("a6_fault_binding_changed")
            deadline = binding.get("absolute_deadline_ms")
            self._armed[case_id] = ExecutedCase(
                case_id=case_id, request_id=request_id, binding=dict(binding),
                source_node_id=source,
                armed_reservation_count=self.reservation_count(request_id),
                source_route=source_route,
                # The command deadline is monotonic; publish it on the wall clock.
                deadline_unix_ms=(None if type(deadline) is not int else int(
                    time.time() * 1_000 + deadline - time.monotonic() * 1_000)),
            )
        return {
            "admitted_through_ordinary_gateway": True,
            "request_in_flight": True,
            "source_attempt": binding["source_attempt"],
        }

    def armed(self, case_id: str) -> ExecutedCase:
        with self._lock:
            record = self._armed.get(case_id)
        if record is None:
            raise FaultRejected("a6_fault_not_armed")
        current = self.router.a6_controlled_fault_binding(record.request_id)
        if current is not None and current != record.binding:
            raise FaultRejected("a6_fault_binding_changed")
        return record

    def complete(self, record: ExecutedCase, post_state: Mapping[str, Any]) -> dict[str, Any]:
        record.post_state = dict(post_state)
        with self._lock:
            self._armed.pop(record.case_id, None)
            self._executed.append(record)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(record)
            except Exception:
                pass  # an observer can never change a measured fault
        return dict(post_state)

    @property
    def executed(self) -> tuple[ExecutedCase, ...]:
        with self._lock:
            return tuple(self._executed)

    # -- real peer and path faults ----------------------------------------------

    def signal_node(self, node_id: str, signum: int, route: Any | None = None) -> None:
        """Signal one node: its remote worker over ssh, else its owned process group."""

        source = self._concrete_route() if route is None else route
        peers = getattr(source, "_peers", None)
        peer = None if not isinstance(peers, Mapping) else peers.get(node_id)
        if getattr(peer, "process_transport", None) == "ssh":
            self._signal_remote_worker(source, peer, node_id, signum)
        else:
            sessions = getattr(source, "_sessions", None)
            session = None if not isinstance(sessions, Mapping) else sessions.get(node_id)
            process = getattr(session, "_process", None)
            deliver = getattr(process, "_signal_process_group", None)
            if not callable(deliver) or deliver(signum) is not True:
                raise FaultRejected("a6_fault_peer_signal_refused")
        with self._lock:
            if signum == signal.SIGSTOP:
                self._stopped_nodes[node_id] = source
            elif signum in (signal.SIGCONT, signal.SIGKILL):
                self._stopped_nodes.pop(node_id, None)

    def _signal_remote_worker(self, source: Any, peer: Any, node_id: str,
                              signum: int) -> None:
        """Signal the hello-verified worker on the peer's host, never the ssh client."""

        reader = getattr(source, "process_id", None)
        try:
            process_id = reader(node_id) if callable(reader) else None
        except Exception as error:
            raise FaultRejected("a6_fault_remote_process_identity_unavailable") from error
        plan = getattr(source, "_plan", None)
        run_id = plan.get("run_id") if isinstance(plan, Mapping) else None
        command = remote_signal_command(signum, process_id, run_id)
        try:
            argv = _peer_process_argv(peer, command)
        except ControllerError as error:
            raise FaultRejected("a6_fault_peer_signal_refused") from error
        if not argv or argv[0] != "ssh":
            raise FaultRejected("a6_fault_peer_signal_refused")
        returncode = self._remote_runner(argv)
        if returncode == REMOTE_IDENTITY_MISMATCH_EXIT:
            raise FaultRejected("a6_fault_remote_process_identity_mismatch")
        if returncode != 0:
            raise FaultRejected("a6_fault_peer_signal_refused")

    def resume_stopped_nodes(self) -> None:
        with self._lock:
            stopped = sorted(self._stopped_nodes.items(), key=lambda item: item[0])
        for node_id, route in stopped:
            try:
                self.signal_node(node_id, signal.SIGCONT, route)
            except FaultRejected:
                pass

    def fail_source(self, record: ExecutedCase) -> dict[str, Any]:
        """Kill (or sever) the source peer and measure the route's own incident."""

        route = record.source_route
        baseline = {item.get("incident_id") for item in self.incidents(route)}
        record.failure_marker = self.gateway_committed(record.request_id)
        self.signal_node(record.source_node_id, signal.SIGKILL, route)
        identity = {name: record.binding[name] for name in IDENTITY_FIELDS}

        def observed() -> dict[str, Any] | None:
            if not self.router.a6_source_failure_observed(record.binding):
                return None
            for incident in self.incidents(route):
                if (incident.get("incident_id") not in baseline
                        and incident.get("request_id") == record.request_id
                        and incident.get("subject_id") == record.source_node_id
                        and incident.get("scope") == "request"
                        and incident.get("fatal_accepted") is False
                        and _incident_identity_matches(incident, identity)):
                    return incident
            return None

        incident = self.wait(observed)
        if incident is None:
            raise FaultRejected("a6_fault_source_failure_unobserved")
        return incident

    def successor_nodes(self, record: ExecutedCase, attempt: int) -> list[str]:
        """Nodes of the successor the qualifier was actually asked to qualify."""

        if self.qualifier is None:
            raise FaultRejected("a6_fault_qualifier_unavailable")
        # M16 mints successor path attempts as source path attempt + recovery
        # attempt, so this identifies exactly the successor of ``attempt``.
        expected_path_attempt = record.binding["path_attempt"] + attempt
        facts = self.wait(lambda: (
            (lambda f: f if f and f.get("successor_path_attempt") == expected_path_attempt
             else None)(self.qualifier.issued_qualification_facts(record.request_id))))
        if facts is None:
            raise FaultRejected("a6_fault_successor_unobserved")
        placements = self.placement_nodes()
        nodes = sorted({placements[item] for item in facts["successor_placement_ids"]
                        if item in placements} - {record.source_node_id})
        if not nodes:
            raise FaultRejected("a6_fault_successor_nodes_unavailable")
        return nodes

    def successor_challenge_facts(self, record: ExecutedCase) -> dict[str, Any]:
        """Why no successor attempt started, as the qualifier measured it.

        The successor's physical challenge runs before its prefill. When that
        challenge failed (or is still running when the seam's bounded wait
        ends), these scalars name its outcome, its error and how any challenged
        node's control process exited (for an ssh peer: the ssh client's exit
        status and last stderr line), so a refused case says whether its
        successor never ran. Diagnostics only: nothing here is compared with
        the case contract.
        """

        reader = getattr(self.qualifier, "physical_challenge_outcome", None)
        try:
            outcome = reader(record.request_id) if callable(reader) else None
        except Exception:
            outcome = None
        if not isinstance(outcome, Mapping):
            outcome = {}
        exits = outcome.get("participant_exits")
        transport = ";".join(
            f"{item.get('node_id')}:{item.get('returncode')}:"
            f"{item.get('stderr_last_line') or ''}"
            for item in (exits if isinstance(exits, (list, tuple)) else ())
            if isinstance(item, Mapping)
        )
        error = outcome.get("error")
        return {
            "successor_challenge_outcome": outcome.get("outcome"),
            "successor_challenge_error": error if isinstance(error, str) else None,
            "successor_transport_exit": transport or None,
            "recovery_phase": self.view(record.request_id).get("recovery_phase"),
        }

    def wait_phase(self, record: ExecutedCase, phase: str, attempts: int | None = None) -> bool:
        return bool(self.wait(lambda: (
            (lambda v: v.get("recovery_phase") == phase and (
                attempts is None or v.get("recovery_attempts") == attempts))(
                self.view(record.request_id)))))

    def wait_terminal(self, record: ExecutedCase, seconds: float | None = None) -> dict[str, Any]:
        def settled() -> dict[str, Any] | None:
            view = self.view(record.request_id)
            if (view.get("terminal_status") in _TERMINAL_STATUSES
                    or view.get("terminal_blocked_reason") is not None
                    or view.get("retained") is False):
                return view
            return None
        return self.wait(settled, seconds) or self.view(record.request_id)

    def cancel(self, record: ExecutedCase) -> bool:
        """Cancel the armed request through the gateway's own cancellation path.

        Every ordinary ASGI submission mints a session owner token the fault
        control never holds, so ``service.cancel(request_id)`` would always be
        refused (``session_owner_mismatch``). The grant-gated fault control
        acts as the product operator for the one request it armed: it skips
        only the owner-token comparison and runs the exact session
        cancellation the owner's request would have run.
        """

        get_session = getattr(self.service, "_get_session", None)
        cancel_session = getattr(self.service, "_cancel_session", None)
        if not callable(get_session) or not callable(cancel_session):
            return False
        try:
            return bool(cancel_session(get_session(record.request_id)))
        except Exception:
            return False

    def start_cancel(self, record: ExecutedCase) -> Callable[[float], bool]:
        """Run :meth:`cancel` concurrently; the returned join yields its result.

        A cancellation fans out to the request's physical participants and
        waits for their cleanup proof; a seam that stalled those participants
        must be able to observe the linearized cancellation and resume them
        while that fan-out is still in flight.
        """

        result: list[bool] = []
        thread = threading.Thread(target=lambda: result.append(self.cancel(record)),
                                  name="a6-fault-cancel", daemon=True)
        thread.start()

        def join(seconds: float) -> bool:
            thread.join(timeout=seconds)
            return bool(result and result[0])

        return join

    def arm_qualification_fault(self, record: ExecutedCase, kind: str) -> None:
        arm = getattr(self.qualifier, "arm_controlled_fault", None)
        if not callable(arm):
            raise FaultRejected("a6_fault_qualifier_unavailable")
        try:
            arm(request_id=record.request_id, kind=kind, grant=self._grant)
        except (PermissionError, ValueError) as error:
            raise FaultRejected("a6_fault_qualifier_refused") from error

    def request_stop(self) -> bool:
        if self._request_stop is None:
            return False
        self._request_stop()
        return bool(self._stop_requested()) if self._stop_requested else True


class ProductCaseSeam:
    """One frozen case bound to real product faults and measurements."""

    def __init__(self, case_id: str, target: LiveProductFaultTarget) -> None:
        if case_id not in INJECTION_POINTS:
            raise ValueError("a6_fault_case_unknown")
        self.case_id = case_id
        self.seam_id = INJECTION_POINTS[case_id].seam_id
        self._target = target
        self._apply = getattr(self, f"_apply_{case_id}")

    def arm(self, *, case_id: str) -> Mapping[str, Any]:
        if case_id != self.case_id:
            raise FaultRejected("a6_fault_case_unavailable")
        return self._target.arm(case_id)

    def apply(self, *, case_id: str) -> Mapping[str, Any]:
        if case_id != self.case_id:
            raise FaultRejected("a6_fault_case_unavailable")
        target = self._target
        record = target.armed(case_id)
        try:
            state = self._apply(record)
        finally:
            target.resume_stopped_nodes()
        return target.complete(record, state)

    # -- positive ------------------------------------------------------------

    def _apply_ordinary_browser_source_failure_recovery(self, record):
        target = self._target
        # Continuity is claimed only for a nonzero committed prefix: give an
        # in-flight request a short grace to commit one. A zero prefix is not
        # refused here; it is measured, and the case observation then fails.
        target.wait(lambda: (lambda m: m is not None and m[1] >= 1)(
            target.gateway_committed(record.request_id)), seconds=2.0)
        incident = target.fail_source(record)
        view = target.wait(lambda: (lambda v: v if (
            v.get("recovery_required") or v.get("recovery_attempts", 0) >= 1
            or v.get("recovery_phase") not in (None, "active")) else None)(
            target.view(record.request_id))) or {}
        return {
            "source_failure_observed": True,
            "recovery_required": bool(view),
            "actual_incident": incident,
        }

    # -- qualifier-issued refusals ---------------------------------------------

    def _qualification_case(self, record, kind, refusal):
        target = self._target
        target.arm_qualification_fault(record, kind)
        target.fail_source(record)
        facts = target.wait(lambda: (lambda f: f if f and f.get("controlled_fault") == kind
                                     else None)(
            target.qualifier.issued_qualification_facts(record.request_id)))
        view = target.wait(lambda: (lambda v: v if v.get("recovery_failure_reason")
                                    else None)(target.view(record.request_id))) or {}
        state = {
            "source_failure_observed": True,
            "successor_candidate_reserved": facts is not None,
            "recovery_refused": view.get("recovery_failure_reason") == refusal,
            "measured_refusal_code": view.get("recovery_failure_reason"),
        }
        facts = facts or {}
        if kind == "incompatible":
            state["qualification_compatible"] = facts.get("compatibility_matches_source")
        elif kind == "stale":
            expiry, measured = facts.get("expiry_unix_ms"), facts.get("measured_at_unix_ms")
            state["qualification_freshness"] = (
                "stale" if isinstance(expiry, int) and isinstance(measured, int)
                and expiry <= measured else "current" if facts else None)
        elif kind == "foreign":
            state["qualification_request_binding_matches"] = facts.get("request_binding_matches")
        elif kind == "unexecuted":
            state["physical_challenge"] = (
                "not_executed" if facts.get("physical_challenge") == "unexecuted"
                else facts.get("physical_challenge"))
        else:
            state["path_attempt_matches_reservation"] = (
                None if not facts else
                facts.get("issued_path_attempt") == facts.get("successor_path_attempt"))
        return state

    def _apply_incompatible_successor(self, record):
        return self._qualification_case(record, *QUALIFICATION_CASES["incompatible_successor"])

    def _apply_stale_qualification(self, record):
        return self._qualification_case(record, *QUALIFICATION_CASES["stale_qualification"])

    def _apply_foreign_qualification(self, record):
        return self._qualification_case(record, *QUALIFICATION_CASES["foreign_qualification"])

    def _apply_unexecuted_qualification(self, record):
        return self._qualification_case(record, *QUALIFICATION_CASES["unexecuted_qualification"])

    def _apply_mismatched_path_attempt(self, record):
        return self._qualification_case(record, *QUALIFICATION_CASES["mismatched_path_attempt"])

    # -- successor reservation and execution -----------------------------------

    def _apply_no_successor(self, record):
        target = self._target
        if not target.router.a6_withhold_recovery_successors(record.request_id):
            raise FaultRejected("a6_fault_candidates_not_withheld")
        target.fail_source(record)
        view = target.wait_terminal(record)
        reason = view.get("recovery_failure_reason")
        return {
            "source_failure_observed": True,
            "candidate_available": reason != "recovery_successor_unavailable",
            "truthful_abort_required": (
                reason == "recovery_successor_unavailable"
                and view.get("terminal_status") in (None, "FAILED")),
        }

    def _apply_successor_prefill_failure(self, record):
        """Fail the first successor during prefill; no further successor exists.

        Owner decision (A6 c0dc4350-a1, option A): the case proves the
        bounded abort when the failed successor has no compatible
        replacement. On a deployment with a third placement the router would
        legitimately retry onto it, so once the first successor's prefill
        started and before it is killed, every remaining placement is
        withheld from this request's next attempt. Withholding only narrows
        the next attempt's candidates; the running attempt is unaffected.
        """

        target = self._target
        target.fail_source(record)
        started = target.wait_phase(record, "recovery_prefill", attempts=1)
        injected = False
        withheld = False
        if started:
            nodes = target.successor_nodes(record, 1)
            if not target.router.a6_withhold_recovery_successors(record.request_id):
                raise FaultRejected("a6_fault_candidates_not_withheld")
            withheld = True
            for node in nodes:
                target.signal_node(node, signal.SIGKILL)
                injected = True
        state = {
            "source_failure_observed": True,
            "successor_attempt_started": started,
            "successor_failure_injected": injected,
            "successors_withheld": withheld,
        }
        if not started:
            # The successor never reached prefill: say why (its challenge).
            state.update(target.successor_challenge_facts(record))
        return state

    def _apply_cancellation_before_recovery(self, record):
        target = self._target
        # Hold the source first: stopping the other peers takes an ssh round
        # trip per peer, during which an unstalled source can finish the whole
        # request, and then there is no in-flight request left to fail. A
        # stopped source still runs no further work and is then killed (the
        # same real source failure as every case); the remaining peers are
        # stalled so the successor challenge cannot run ahead of the owner's
        # cancellation. Every stalled peer is resumed on return.
        target.signal_node(record.source_node_id, signal.SIGSTOP, record.source_route)
        others = sorted(set(target.placement_nodes().values()) - {record.source_node_id})
        for node in others:
            target.signal_node(node, signal.SIGSTOP)
        target.fail_source(record)
        before = target.view(record.request_id)
        accepted = target.cancel(record)
        after = target.view(record.request_id)
        not_recovered = all(v.get("recovery_phase") in _PRE_RECOVERY_PHASES
                            for v in (before, after))
        return {
            "source_failure_observed": True,
            "cancellation_observed_before_recovery": (
                accepted and after.get("cancellation_requested") is True and not_recovered),
            # Measured inputs of the derived fact, so a refusal explains itself.
            "before_phase": before.get("recovery_phase"),
            "after_phase": after.get("recovery_phase"),
            "cancel_accepted": accepted,
            "cancellation_requested": after.get("cancellation_requested"),
        }

    def _apply_cancellation_during_prefill(self, record):
        target = self._target
        target.fail_source(record)
        observed = False
        if target.wait_phase(record, "recovery_prefill", attempts=1):
            for node in target.successor_nodes(record, 1):
                target.signal_node(node, signal.SIGSTOP)
            # The stalled successor keeps the recovery in prefill while the
            # owner's cancellation linearizes; it is resumed before the
            # cancellation's cleanup fan-out has to reach it.
            join = target.start_cancel(record)
            linearized = target.wait(lambda: (lambda v: v if (
                v.get("cancellation_linearized") is True
                or v.get("cancellation_requested") is True
                or v.get("terminal_status") in _TERMINAL_STATUSES
                or v.get("retained") is False) else None)(
                target.view(record.request_id))) or {}
            in_prefill = linearized.get("recovery_phase") == "recovery_prefill"
            target.resume_stopped_nodes()
            accepted = join(target.wait_seconds)
            view = target.view(record.request_id)
            observed = (accepted and in_prefill
                        and (view.get("cancellation_requested") is True
                             or view.get("terminal_status") == "CANCELLED"))
        return {"source_failure_observed": True,
                "cancellation_observed_during_prefill": observed}

    def _apply_timeout_during_recovery(self, record):
        target = self._target
        target.fail_source(record)
        exceeded = False
        if target.wait_phase(record, "recovery_prefill", attempts=1):
            for node in target.successor_nodes(record, 1):
                target.signal_node(node, signal.SIGSTOP)
            # The router's prefill/progress recovery deadline interrupts the
            # stopped successor (``RecoveryPhaseDeadlines``). Hold the successor
            # until the router reports that it interrupted the stalled work,
            # then resume it so the interruption's cleanup can be proven.
            fired = target.wait(lambda: (lambda v: v if (
                v.get("recovery_deadline_exceeded") is True
                or v.get("recovery_failure_reason") is not None
                or v.get("terminal_status") in _TERMINAL_STATUSES
                or v.get("retained") is False) else None)(
                target.view(record.request_id)), seconds=RECOVERY_DEADLINE_WAIT_SECONDS) or {}
            target.resume_stopped_nodes()
            if fired:
                view = target.wait(lambda: (lambda v: v if (
                    v.get("recovery_failure_reason") is not None
                    or v.get("terminal_blocked_reason") is not None) else None)(
                    target.view(record.request_id)), seconds=RECOVERY_DEADLINE_WAIT_SECONDS) or {}
                exceeded = (fired.get("recovery_deadline_exceeded") is True
                            and view.get("recovery_failure_reason") == "recovery_timeout")
        return {"source_failure_observed": True, "recovery_deadline_exceeded": exceeded}

    def _apply_exhausted_recovery_attempts(self, record):
        target = self._target
        target.fail_source(record)
        # Count only attempts whose successor really started: the router
        # increments its attempt counter before reserving, so a deployment
        # with no second disjoint successor reaches "2 attempts" without a
        # second successor ever running.
        killed: list[tuple[int, tuple[str, ...]]] = []
        for attempt in (1, 2):
            if not target.wait_phase(record, "recovery_prefill", attempts=attempt):
                break
            nodes = tuple(target.successor_nodes(record, attempt))
            for node in nodes:
                target.signal_node(node, signal.SIGKILL)
            killed.append((attempt, nodes))
        # The last attempt's terminal can come from the router's recovery
        # deadline, not from the killed successor, so wait that long for it.
        view = target.wait_terminal(record, seconds=RECOVERY_DEADLINE_WAIT_SECONDS)
        attempts = view.get("recovery_attempts")
        distinct = len(killed) == 2 and not set(killed[0][1]) & set(killed[1][1])
        return {
            "source_failure_observed": True,
            "attempts_used": len(killed),
            "attempts_exhausted": (distinct and attempts == 2
                                   and view.get("terminal_status") == "FAILED"),
            "successor_nodes_failed": [list(nodes) for _attempt, nodes in killed],
            # Measured terminal view, so a refusal explains itself.
            "successor_nodes_killed": ";".join(",".join(nodes) for _attempt, nodes in killed),
            "recovery_attempts": attempts,
            "terminal_status": view.get("terminal_status"),
            "terminal_blocked_reason": view.get("terminal_blocked_reason"),
            "recovery_failure_reason": view.get("recovery_failure_reason"),
        }

    def _apply_stale_source_output_after_fencing(self, record):
        target = self._target
        source_identity = target.view(record.request_id).get("route_identity")
        target.fail_source(record)
        coordinator = getattr(target.router, "_coordinator", None)
        # The router fences the source immediately before recovery prefill; a
        # later phase or a terminal is also past the fence.
        fenced = coordinator is not None and bool(target.wait(lambda: (
            lambda v: v.get("recovery_phase") in _FENCED_PHASES
            or v.get("terminal_status") in _TERMINAL_STATUSES)(
            target.view(record.request_id))))
        before = target.view(record.request_id).get("token_count")
        accepted = (coordinator.authorizes_route_identity(record.request_id, source_identity)
                    if fenced and source_identity is not None else None)
        after = target.view(record.request_id).get("token_count")
        return {
            "source_failure_observed": True,
            "source_fenced": bool(fenced),
            "late_publication_attempted": accepted is not None,
            "late_publication_rejected": accepted is False and before == after,
        }

    # -- cleanup and shutdown ---------------------------------------------------

    def _apply_cleanup_reservation_release(self, record):
        target = self._target
        view = target.wait_terminal(record, seconds=120.0)
        after = target.reservation_count(record.request_id)
        before = record.armed_reservation_count
        return {
            # A terminal-blocked or released request is terminal too: a
            # blocked request has no terminal status, only its blocked reason.
            "request_terminal": (view.get("terminal_status") in _TERMINAL_STATUSES
                                 or view.get("terminal_blocked_reason") is not None
                                 or view.get("retained") is False),
            "release_observed": isinstance(before, int) and after == 0 and before > 0,
            "release_count_positive": isinstance(before, int) and before > 0,
            # Measured terminal view, so a refusal explains itself.
            "terminal_status": view.get("terminal_status"),
            "terminal_blocked_reason": view.get("terminal_blocked_reason"),
            "reservations_before": before,
            "reservations_after": after,
        }

    def _apply_cleanup_authority_failure(self, record):
        """Owner ruling 2026-10-06: the case proves the product's real shape.

        Fail the source and let the ordinary product start its successor. The
        router's A6 completion-cleanup hold, armed here before the fault and
        inert otherwise, holds the successor at its cleanup authorization;
        the seam stalls the successor's peers there and only then releases
        the hold, so the stall always lands before cleanup can be proven. The
        cleanup authority must then refuse: no cutover is committed, the
        successor's buffered suffix is discarded unpublished, and the request
        ends TERMINAL_BLOCKED with one failure notice. The stalled peers are
        resumed afterwards.
        """

        target = self._target
        router = target.router
        arm = getattr(router, "a6_arm_completion_cleanup_hold", None)
        release = getattr(router, "a6_release_completion_cleanup_hold", None)
        if not callable(arm) or not callable(release):
            raise FaultRejected("a6_fault_cleanup_hold_unavailable")
        target.wait(lambda: (lambda m: m is not None and m[1] >= 1)(
            target.gateway_committed(record.request_id)), seconds=2.0)
        if not arm(record.request_id):
            raise FaultRejected("a6_fault_cleanup_hold_unarmed")
        stalled = False
        held: dict[str, Any] = {}
        try:
            target.fail_source(record)
            if target.wait_phase(record, "recovery_prefill", attempts=1):
                nodes = target.successor_nodes(record, 1)

                def cleanup_held() -> dict[str, Any] | None:
                    # A successor that failed or was replaced before reaching
                    # its cleanup authorization ends the wait.
                    view = target.view(record.request_id)
                    if view.get("recovery_cleanup_held") is True:
                        return view
                    if (view.get("recovery_phase") != "recovery_prefill"
                            or view.get("terminal_status") in _TERMINAL_STATUSES
                            or view.get("terminal_blocked_reason") is not None
                            or view.get("retained") is False):
                        return {"late": True, **view}
                    return None

                held = target.wait(cleanup_held, seconds=120.0, poll_seconds=0.001) or {}
                if held and not held.get("late"):
                    for node in nodes:
                        target.signal_node(node, signal.SIGSTOP)
                        stalled = True
        finally:
            release(record.request_id)
        view = (target.wait(lambda: (lambda v: v if (
            v.get("terminal_blocked_reason")
            or v.get("terminal_status") in _TERMINAL_STATUSES
            or v.get("retained") is False) else None)(target.view(record.request_id)),
                            seconds=120.0) if stalled else None) or {}
        source_attempt = record.binding["source_attempt"]
        committed = target.committed_route_identity(record.request_id) or {}
        cut_over = (committed.get("request_attempt") or 0) > source_attempt
        refused = (stalled and not cut_over
                   and view.get("terminal_blocked_reason") is not None
                   and view.get("terminal_status") is None)
        delivered = target.gateway_committed(record.request_id)
        held_tokens = held.get("token_count")
        suffix_discarded = (
            refused
            and isinstance(held.get("recovery_output_count"), int)
            and held["recovery_output_count"] >= 1
            and isinstance(held_tokens, int)
            # Nothing the successor generated reached the request or the
            # client: the delivered prefix is exactly the pre-recovery one.
            and view.get("token_count") == held_tokens
            and view.get("cursor") == held_tokens
            and delivered is not None and delivered[1] == held_tokens)
        state = {
            "cleanup_authority_refused": refused,
            "cutover_committed": cut_over,
            "unpublishable_suffix_discarded": suffix_discarded,
            # Measured inputs of the derived facts, so a refusal explains itself.
            "successor_stalled": stalled,
            "cleanup_held": held.get("recovery_cleanup_held") is True,
            "successor_output_count": held.get("recovery_output_count"),
            "phase_at_stall_wait": held.get("recovery_phase"),
            "prefix_token_count": held_tokens,
            "delivered_token_count": None if delivered is None else delivered[1],
            "terminal_status": view.get("terminal_status"),
            "terminal_blocked_reason": view.get("terminal_blocked_reason"),
        }
        if not held:
            # The successor never reached prefill: say why (its challenge).
            state.update(target.successor_challenge_facts(record))
        return state

    def _apply_shutdown_during_recovery(self, record):
        target = self._target
        target.fail_source(record)
        target.wait(lambda: (lambda v: v if v.get("recovery_phase") not in (None, "active")
                             else None)(target.view(record.request_id)))
        return {"source_failure_observed": True, "shutdown_observed": target.request_stop()}


def build_product_case_seams(target: LiveProductFaultTarget) -> dict[str, ProductCaseSeam]:
    seams = [ProductCaseSeam(case_id, target) for case_id in INJECTION_POINTS]
    return {seam.seam_id: seam for seam in seams}


def request_gateway_service(app: Any) -> Any | None:
    """The request gateway service behind the product application, if any."""

    gateway = getattr(app, "_request_gateway_app", None)
    return getattr(gateway, "_service", None)


__all__ = [
    "ExecutedCase",
    "IDENTITY_FIELDS",
    "LiveProductFaultTarget",
    "ProductCaseSeam",
    "QUALIFICATION_CASES",
    "build_product_case_seams",
    "request_gateway_service",
]
