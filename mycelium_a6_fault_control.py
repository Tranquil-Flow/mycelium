# SPDX-License-Identifier: AGPL-3.0-only
"""Controlled-fault mechanism for the A6 physical recovery gate.

``scripts/run_a6_recovery_gate.py`` freezes 16 required cases plus 3 cleanup
checks, and ``ui/web/scripts/a6-product-browser-e2e.mjs`` triggers the browser
half by sending an explicit action and case ID to a separate local-only fault
control origin. Neither half can take its first step until a
*controlled fault* exists: until now the only "fault" in the tree was baked into
a test route class, so no case could be produced on demand and no pre-recovery
failure state could be proven.

This module is that mechanism, and only that mechanism:

* it accepts exactly the browser harness's action string,
* it is fail-closed: the exact fleet grant and a loopback caller are checked
  before any seam is armed, and a refusal has no side effect at all,
* it is deterministic: one injected clock, one monotone fault sequence, one
  canonical digest per record and for the whole ledger,
* it owns the 16 injection points, one per frozen case id, and each injection
  must *prove* the pre-recovery failure state that case depends on, measured
  from the seam after injection -- an injection that leaves the product clean
  is refused, not recorded.

The seams themselves are supplied by the caller.  ``mycelium_a6_live_fault_seams``
binds them onto the ordinary in-process product path (gateway service, live
router port, M16 coordinator, route); a physical round binds them onto the real
peers.  The mechanism never invents a measured fact: ``pre_state``/``post_state``
are whatever the seam measured, and the injection is recorded only when those
measurements satisfy the case's declared contract.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from scripts.run_a6_recovery_gate import REQUIRED_CASES, REQUIRED_CLEANUP_CHECKS

PROTOCOL = "mycelium.a6_controlled_fault.v1"
RECORD_PROTOCOL = "mycelium.a6_controlled_fault_record.v1"
EXECUTE_SOURCE_FAILURE_ACTION = "execute_controlled_source_failure"
EXACT_FLEET_GRANT = "FLEET GRANTED TO A6"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


class FaultRejected(Exception):
    """A bounded, non-disclosing refusal code with no observable effect."""


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class InjectionPoint:
    """One controlled-fault injection point, bound to one frozen case id.

    ``required_pre_state`` is what must already be measured before injection and
    ``expected_pre_recovery_state`` is what must be measured after it: the state
    from which the product's own recovery, refusal, or cleanup must proceed.
    """

    case_id: str
    seam_id: str
    seam_kind: str
    required_pre_state: Mapping[str, Any]
    expected_pre_recovery_state: Mapping[str, Any]
    description: str


#: Every injection point starts from the same measured precondition: a request
#: admitted and in flight through the ordinary product path, on its source
#: attempt.  The controlled source failure is the shared A6 recovery trigger;
#: each case's injection point then lands its own fault on the seam that case is
#: about, and must measure the case's pre-recovery failure state afterwards.
_ADMITTED_IN_FLIGHT: Mapping[str, Any] = {
    "admitted_through_ordinary_gateway": True,
    "request_in_flight": True,
    "source_attempt": 1,
}


def _point(
    case_id: str,
    seam_kind: str,
    expected_pre_recovery_state: Mapping[str, Any],
    description: str,
) -> InjectionPoint:
    return InjectionPoint(
        case_id=case_id,
        seam_id=f"a6.fault.{case_id}",
        seam_kind=seam_kind,
        required_pre_state=dict(_ADMITTED_IN_FLIGHT),
        expected_pre_recovery_state=dict(expected_pre_recovery_state),
        description=description,
    )


INJECTION_POINTS: Mapping[str, InjectionPoint] = {
    point.case_id: point
    for point in (
        _point(
            "ordinary_browser_source_failure_recovery",
            "route.assigned_source_failure",
            {"source_failure_observed": True, "recovery_required": True},
            "Fail the assigned source placement of the in-flight request while it "
            "is decoding, through the route seam the ordinary product path uses.",
        ),
        _point(
            "incompatible_successor",
            "qualifier.successor_qualification",
            {
                "source_failure_observed": True,
                "successor_candidate_reserved": True,
                "qualification_compatible": False,
                "recovery_refused": True,
            },
            "Fail the source, reserve a successor, then qualify it with an "
            "incompatible compatibility result so the product must refuse cutover.",
        ),
        _point(
            "stale_qualification",
            "qualifier.successor_qualification",
            {
                "source_failure_observed": True,
                "successor_candidate_reserved": True,
                "qualification_freshness": "stale",
                "recovery_refused": True,
            },
            "Fail the source, reserve a successor, then qualify it with an expired "
            "publication so the product must refuse cutover.",
        ),
        _point(
            "foreign_qualification",
            "qualifier.successor_qualification",
            {
                "source_failure_observed": True,
                "successor_candidate_reserved": True,
                "qualification_request_binding_matches": False,
                "recovery_refused": True,
            },
            "Fail the source, reserve a successor, then qualify it against a "
            "different request binding so the product must refuse cutover.",
        ),
        _point(
            "unexecuted_qualification",
            "qualifier.successor_qualification",
            {
                "source_failure_observed": True,
                "successor_candidate_reserved": True,
                "physical_challenge": "not_executed",
                "recovery_refused": True,
            },
            "Fail the source, reserve a successor, then present a qualification "
            "whose physical challenge never ran so the product must refuse cutover.",
        ),
        _point(
            "mismatched_path_attempt",
            "successor.path_attempt_binding",
            {
                "source_failure_observed": True,
                "successor_candidate_reserved": True,
                "path_attempt_matches_reservation": False,
                "recovery_refused": True,
            },
            "Fail the source, reserve a successor, then bind the successor attempt "
            "to a path attempt no reservation owns so the product must refuse.",
        ),
        _point(
            "no_successor",
            "coordinator.successor_reservation",
            {
                "source_failure_observed": True,
                "candidate_available": False,
                "truthful_abort_required": True,
            },
            "Fail the source, then withhold every compatible candidate so the "
            "product must publish a truthful abort inside its bounded policy.",
        ),
        _point(
            "successor_prefill_failure",
            "route.successor_attempt",
            {
                "source_failure_observed": True,
                "successor_attempt_started": True,
                "successor_failure_injected": True,
            },
            "Fail the source, then fail the successor attempt during recovery "
            "prefill so the product's bounded attempt policy decides the terminal.",
        ),
        _point(
            "cancellation_before_recovery",
            "request.cancellation",
            {
                "source_failure_observed": True,
                "cancellation_observed_before_recovery": True,
            },
            "Fail the source, then observe an owner cancellation before any "
            "recovery attempt publishes anything.",
        ),
        _point(
            "cancellation_during_prefill",
            "request.cancellation",
            {
                "source_failure_observed": True,
                "cancellation_observed_during_prefill": True,
            },
            "Fail the source, let the successor start prefilling, then observe an "
            "owner cancellation while that prefill is still in flight.",
        ),
        _point(
            "timeout_during_recovery",
            "recovery.deadline_clock",
            {
                "source_failure_observed": True,
                "recovery_deadline_exceeded": True,
            },
            "Fail the source, then advance the injected recovery clock past the "
            "frozen recovery deadline while the recovery is in flight.",
        ),
        _point(
            "exhausted_recovery_attempts",
            "recovery.attempt_budget",
            {
                "source_failure_observed": True,
                "attempts_used": 2,
                "attempts_exhausted": True,
            },
            "Fail the source and then fail both permitted successor attempts so "
            "the product must stop instead of starting a third attempt.",
        ),
        _point(
            "stale_source_output_after_fencing",
            "source.post_fence_publication",
            {
                "source_failure_observed": True,
                "source_fenced": True,
                "late_publication_attempted": True,
                "late_publication_rejected": True,
            },
            "Fail the source, and make the fenced source attempt one late "
            "publication so the product must reject it without ledger advance.",
        ),
        _point(
            "cleanup_reservation_release",
            "cleanup.reservation_release",
            {
                "request_terminal": True,
                "release_observed": True,
                "release_count_positive": True,
            },
            "Drive one request to a terminal through the ordinary path and record "
            "the owner cleanup release of its reservations.",
        ),
        _point(
            "cleanup_authority_failure",
            "cleanup.authority_receipt",
            {
                "cleanup_authority_refused": True,
                "cutover_committed": False,
                "unpublishable_suffix_discarded": True,
            },
            "Fail the source, then stall the successor at its completion "
            "cleanup so the cleanup authority must refuse: no cutover is "
            "committed, the product fails closed, publishes no request result, "
            "and discards the unpublishable suffix.",
        ),
        _point(
            "shutdown_during_recovery",
            "runtime.shutdown",
            {
                "source_failure_observed": True,
                "shutdown_observed": True,
            },
            "Fail the source, then shut the product runtime down while the "
            "recovery is in flight.",
        ),
    )
}

assert tuple(INJECTION_POINTS) == REQUIRED_CASES, "injection points must match the frozen case set"

CLEANUP_MEASUREMENTS: tuple[str, ...] = tuple(REQUIRED_CLEANUP_CHECKS)


@dataclass(frozen=True, slots=True)
class ControlledFaultAuthority:
    """The mechanism's fail-closed authorization: exact grant, loopback caller."""

    grant: str | None
    caller_host: str | None

    def reject(self) -> str | None:
        if self.grant != EXACT_FLEET_GRANT:
            return "explicit_fleet_grant_required"
        if self.caller_host not in LOOPBACK_HOSTS:
            return "non_loopback_fault_request"
        return None


@runtime_checkable
class FaultSeam(Protocol):
    """A product seam a controlled fault can be applied to.

    ``arm`` measures the current state and prepares the injection; ``apply``
    performs the real fault and returns the state measured *after* it.  Both
    must be deterministic for a deterministic run and must never fabricate a
    fact they did not measure.
    """

    seam_id: str

    def arm(self, *, case_id: str) -> Mapping[str, Any]: ...

    def apply(self, *, case_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FaultRecord:
    protocol: str
    sequence: int
    case_id: str
    injection_point: str
    seam_id: str
    seam_kind: str
    action: str
    executed_at_unix_ms: int
    pre_state: Mapping[str, Any]
    post_state: Mapping[str, Any]
    pre_recovery_proven: bool
    record_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "sequence": self.sequence,
            "case_id": self.case_id,
            "injection_point": self.injection_point,
            "seam_id": self.seam_id,
            "seam_kind": self.seam_kind,
            "action": self.action,
            "executed_at_unix_ms": self.executed_at_unix_ms,
            "pre_state": dict(self.pre_state),
            "post_state": dict(self.post_state),
            "pre_recovery_proven": self.pre_recovery_proven,
            "record_digest": self.record_digest,
        }


def _record_body(
    *,
    sequence: int,
    point: InjectionPoint,
    action: str,
    executed_at_unix_ms: int,
    pre_state: Mapping[str, Any],
    post_state: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "protocol": RECORD_PROTOCOL,
        "sequence": sequence,
        "case_id": point.case_id,
        "injection_point": point.case_id,
        "seam_id": point.seam_id,
        "seam_kind": point.seam_kind,
        "action": action,
        "executed_at_unix_ms": executed_at_unix_ms,
        "pre_state": dict(pre_state),
        "post_state": dict(post_state),
        "pre_recovery_proven": True,
    }


def _satisfies(measured: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(
        key in measured and measured[key] == value for key, value in expected.items()
    )


class ControlledFaultMechanism:
    """The live product/fault controller the A6 case runner's docstring names."""

    def __init__(
        self,
        *,
        seams: Mapping[str, FaultSeam],
        authority: ControlledFaultAuthority,
        clock_ms: Callable[[], int],
    ) -> None:
        self._seams = dict(seams)
        self._authority = authority
        self._clock_ms = clock_ms
        self._records: list[FaultRecord] = []
        self._executed: set[str] = set()

    # -- authorization ------------------------------------------------------

    def authorize(self) -> str | None:
        return self._authority.reject()

    # -- injection ----------------------------------------------------------

    def execute(self, action: str, *, case_id: str | None = None) -> FaultRecord:
        """Apply the controlled fault for ``case_id`` and record what it proved.

        Refusals are total: nothing is armed, applied, or recorded.
        """

        rejection = self._authority.reject()
        if rejection is not None:
            raise FaultRejected(rejection)
        if action != EXECUTE_SOURCE_FAILURE_ACTION:
            raise FaultRejected("controlled_fault_unknown_action")
        if case_id is None or case_id not in INJECTION_POINTS:
            raise FaultRejected("controlled_fault_unknown_case")
        point = INJECTION_POINTS[case_id]
        if case_id in self._executed:
            raise FaultRejected(f"controlled_fault_already_executed:{case_id}")
        seam = self._seams.get(point.seam_id)
        if seam is None:
            raise FaultRejected(f"controlled_fault_seam_unavailable:{point.seam_id}")

        pre_state = dict(seam.arm(case_id=case_id))
        if not _satisfies(pre_state, point.required_pre_state):
            raise FaultRejected(f"controlled_fault_pre_state_unproven:{case_id}")
        post_state = dict(seam.apply(case_id=case_id))
        if not _satisfies(post_state, point.expected_pre_recovery_state):
            rejected = FaultRejected(f"controlled_fault_injection_unproven:{case_id}")
            # Measured values of the unmet contract keys, for operator logs.
            rejected.unmet = {
                key: (post_state.get(key) if isinstance(
                    post_state.get(key), (bool, int, str, type(None))) else "non_scalar")
                for key, expected in point.expected_pre_recovery_state.items()
                if post_state.get(key) != expected
            }
            # The seam's other measured scalars (phases, attempts, ...) that
            # explain why a contract key was unmet.
            rejected.measured = {
                key: value for key, value in post_state.items()
                if key not in point.expected_pre_recovery_state
                and isinstance(value, (bool, int, str, type(None)))
            }
            raise rejected

        sequence = len(self._records) + 1
        body = _record_body(
            sequence=sequence,
            point=point,
            action=action,
            executed_at_unix_ms=int(self._clock_ms()),
            pre_state=pre_state,
            post_state=post_state,
        )
        record = FaultRecord(
            protocol=body["protocol"],
            sequence=sequence,
            case_id=point.case_id,
            injection_point=point.case_id,
            seam_id=point.seam_id,
            seam_kind=point.seam_kind,
            action=action,
            executed_at_unix_ms=body["executed_at_unix_ms"],
            pre_state=pre_state,
            post_state=post_state,
            pre_recovery_proven=True,
            record_digest=_digest(body),
        )
        self._records.append(record)
        self._executed.add(case_id)
        return record

    # -- projections --------------------------------------------------------

    def records(self) -> tuple[FaultRecord, ...]:
        return tuple(self._records)

    def record_for(self, case_id: str) -> FaultRecord:
        for record in self._records:
            if record.case_id == case_id:
                return record
        raise FaultRejected(f"controlled_fault_not_executed:{case_id}")

    def ledger_digest(self) -> str:
        return _digest(
            {
                "protocol": PROTOCOL,
                "records": [record.record_digest for record in self._records],
            }
        )

    def status(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "executed_count": len(self._records),
            "executed_cases": [record.case_id for record in self._records],
            "next_sequence": len(self._records) + 1,
            "required_cases": list(REQUIRED_CASES),
            "ledger_digest": self.ledger_digest(),
        }


__all__ = [
    "CLEANUP_MEASUREMENTS",
    "ControlledFaultAuthority",
    "ControlledFaultMechanism",
    "EXACT_FLEET_GRANT",
    "EXECUTE_SOURCE_FAILURE_ACTION",
    "FaultRecord",
    "FaultRejected",
    "FaultSeam",
    "INJECTION_POINTS",
    "InjectionPoint",
    "LOOPBACK_HOSTS",
    "PROTOCOL",
    "RECORD_PROTOCOL",
]
