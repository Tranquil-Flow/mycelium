# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bind the A6 controlled fault to real peer and path failures.

Every case in ``mycelium_a6_fault_control.INJECTION_POINTS`` starts from the
same measured precondition: one request admitted through the ordinary gateway
and executing on its source attempt. The shared trigger is a real failure of
the request's assigned source peer: the product signals the owned process group
of that node's control session with ``SIGKILL``. For a node the serve process
launched locally this kills the peer; for a node reached through a launched
transport (for example ssh) it severs that path. Either way the route observes
the failure through its own reader and records an A4 scoped runtime incident.

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

import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from mycelium_a6_fault_control import EXACT_FLEET_GRANT, FaultRejected, INJECTION_POINTS

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


@dataclass
class ExecutedCase:
    """One executed product fault, retained for the source-owned observation."""

    case_id: str
    request_id: str
    binding: dict[str, Any]
    source_node_id: str
    armed_reservation_count: int | None
    post_state: dict[str, Any] = field(default_factory=dict)


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
        self._stopped_nodes: set[str] = set()

    # -- reads ---------------------------------------------------------------

    def _concrete_route(self) -> Any:
        current = getattr(self.route, "_current_route_or_raise", None)
        return current() if callable(current) else self.route

    def view(self, request_id: str) -> dict[str, Any]:
        view = self.router.a6_request_view(request_id)
        return {} if view is None else dict(view)

    def incidents(self) -> list[dict[str, Any]]:
        reader = getattr(self._concrete_route(), "a4_scoped_runtime_incidents", None)
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

    def placement_nodes(self) -> dict[str, str]:
        return dict(self.router.a6_placement_node_ids())

    def wait(self, predicate: Callable[[], Any], seconds: float | None = None) -> Any:
        deadline = self._clock() + (self._wait_seconds if seconds is None else seconds)
        while True:
            value = predicate()
            if value:
                return value
            if self._clock() >= deadline:
                return None
            self._sleep(self._poll)

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
        with self._lock:
            prior = self._armed.get(case_id)
            if prior is not None and prior.binding != binding:
                raise FaultRejected("a6_fault_binding_changed")
            self._armed[case_id] = ExecutedCase(
                case_id=case_id, request_id=request_id, binding=dict(binding),
                source_node_id=source,
                armed_reservation_count=self.reservation_count(request_id),
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
        return dict(post_state)

    @property
    def executed(self) -> tuple[ExecutedCase, ...]:
        with self._lock:
            return tuple(self._executed)

    # -- real peer and path faults ----------------------------------------------

    def signal_node(self, node_id: str, signum: int) -> None:
        """Signal the owned process group of one node's control session."""

        sessions = getattr(self._concrete_route(), "_sessions", None)
        session = None if not isinstance(sessions, Mapping) else sessions.get(node_id)
        process = getattr(session, "_process", None)
        deliver = getattr(process, "_signal_process_group", None)
        if not callable(deliver) or deliver(signum) is not True:
            raise FaultRejected("a6_fault_peer_signal_refused")
        with self._lock:
            if signum == signal.SIGSTOP:
                self._stopped_nodes.add(node_id)
            elif signum in (signal.SIGCONT, signal.SIGKILL):
                self._stopped_nodes.discard(node_id)

    def resume_stopped_nodes(self) -> None:
        with self._lock:
            stopped = sorted(self._stopped_nodes)
        for node_id in stopped:
            try:
                self.signal_node(node_id, signal.SIGCONT)
            except FaultRejected:
                pass

    def fail_source(self, record: ExecutedCase) -> dict[str, Any]:
        """Kill (or sever) the source peer and measure the route's own incident."""

        baseline = {item.get("incident_id") for item in self.incidents()}
        self.signal_node(record.source_node_id, signal.SIGKILL)
        identity = {name: record.binding[name] for name in IDENTITY_FIELDS}

        def observed() -> dict[str, Any] | None:
            if not self.router.a6_source_failure_observed(record.binding):
                return None
            for incident in self.incidents():
                if (incident.get("incident_id") not in baseline
                        and incident.get("request_id") == record.request_id
                        and incident.get("subject_id") == record.source_node_id
                        and incident.get("scope") == "request"
                        and incident.get("fatal_accepted") is False
                        and all(incident.get(name) == value for name, value in identity.items()
                                if name in incident)):
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
        cancel = getattr(self.service, "cancel", None)
        if not callable(cancel):
            return False
        try:
            return bool(cancel(record.request_id))
        except Exception:
            return False

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
        target = self._target
        target.fail_source(record)
        started = target.wait_phase(record, "recovery_prefill", attempts=1)
        injected = False
        if started:
            for node in target.successor_nodes(record, 1):
                target.signal_node(node, signal.SIGKILL)
                injected = True
        return {
            "source_failure_observed": True,
            "successor_attempt_started": started,
            "successor_failure_injected": injected,
        }

    def _apply_cancellation_before_recovery(self, record):
        target = self._target
        # Stall every other peer first so the successor challenge cannot run
        # ahead of the owner's cancellation; they are resumed on return.
        others = sorted(set(target.placement_nodes().values()) - {record.source_node_id})
        for node in others:
            target.signal_node(node, signal.SIGSTOP)
        target.fail_source(record)
        before = target.view(record.request_id)
        accepted = target.cancel(record)
        after = target.view(record.request_id)
        not_recovered = all(v.get("recovery_phase") in (None, "active", "successor_pending")
                            for v in (before, after))
        return {
            "source_failure_observed": True,
            "cancellation_observed_before_recovery": (
                accepted and after.get("cancellation_requested") is True and not_recovered),
        }

    def _apply_cancellation_during_prefill(self, record):
        target = self._target
        target.fail_source(record)
        observed = False
        if target.wait_phase(record, "recovery_prefill", attempts=1):
            for node in target.successor_nodes(record, 1):
                target.signal_node(node, signal.SIGSTOP)
            accepted = target.cancel(record)
            view = target.view(record.request_id)
            observed = (accepted and view.get("cancellation_requested") is True
                        and view.get("recovery_phase") == "recovery_prefill")
        return {"source_failure_observed": True,
                "cancellation_observed_during_prefill": observed}

    def _apply_timeout_during_recovery(self, record):
        target = self._target
        target.fail_source(record)
        exceeded = False
        if target.wait_phase(record, "recovery_prefill", attempts=1):
            for node in target.successor_nodes(record, 1):
                target.signal_node(node, signal.SIGSTOP)
            view = target.wait(lambda: (lambda v: v if (
                v.get("recovery_failure_reason") == "recovery_timeout"
                or v.get("terminal_error_code") == "deadline_exceeded") else None)(
                target.view(record.request_id)), seconds=120.0) or {}
            exceeded = bool(view)
        return {"source_failure_observed": True, "recovery_deadline_exceeded": exceeded}

    def _apply_exhausted_recovery_attempts(self, record):
        target = self._target
        target.fail_source(record)
        for attempt in (1, 2):
            if not target.wait_phase(record, "recovery_prefill", attempts=attempt):
                break
            for node in target.successor_nodes(record, attempt):
                target.signal_node(node, signal.SIGKILL)
        view = target.wait_terminal(record)
        attempts = view.get("recovery_attempts")
        return {
            "source_failure_observed": True,
            "attempts_used": attempts,
            "attempts_exhausted": (attempts == 2 and view.get("terminal_status") == "FAILED"),
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
            "request_terminal": view.get("terminal_status") in _TERMINAL_STATUSES,
            "release_observed": isinstance(before, int) and after == 0 and before > 0,
            "release_count_positive": isinstance(before, int) and before > 0,
        }

    def _apply_cleanup_authority_failure(self, record):
        target = self._target
        others = sorted(set(target.placement_nodes().values()) - {record.source_node_id})
        coordinator = getattr(target.router, "_coordinator", None)
        if coordinator is None:
            raise FaultRejected("a6_fault_coordinator_unavailable")
        # Lose the participants once decoding is done and request cleanup has
        # begun, so the route cannot prove cleanup: the cleanup authority must
        # refuse to publish a terminal for work it cannot prove released.
        done = target.wait(lambda: coordinator.phase(record.request_id) == "cleanup",
                           seconds=120.0)
        if done:
            for node in others or [record.source_node_id]:
                target.signal_node(node, signal.SIGKILL)
        view = target.wait_terminal(record, seconds=120.0)
        refused = view.get("terminal_blocked_reason") is not None or (
            view.get("recovery_failure_reason") == "cleanup_authority_failed")
        return {
            "cleanup_authority_refused": bool(done) and refused,
            # Nothing past the gateway-delivered cursor is ever published.
            "unpublishable_suffix_discarded": (
                refused and view.get("cursor") == view.get("token_count")),
        }

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
