# SPDX-License-Identifier: AGPL-3.0-only
"""Single source of truth for every request/control-path timing budget.

Packet ``wp2-1`` (``PLAN.md`` 5.2 item 1).  Every timeout, deadline, reserve,
lease, TTL and poll interval that the request/control path consumes is recorded
here exactly once, together with its unit, the owning module, the reason the
value is what it is, and the constant/symbol it replaces.

Rules this module obeys:

* **Values never change behaviour.**  Every value below is byte-identical to the
  shipped literal it replaces, with one deliberate exception: the peer's
  advertised cleanup bound (``peer_advertised_maximum_total_cleanup_ms``).
  Packet ``wp5-d9`` corrected that value away from the shipped 2_000 ms literal,
  which exceeded the route's fanout window and violated the composition
  invariant this module exists to enforce (the round-50 defect: advertised
  bound > route fanout window).  Merge ``a13-resolve`` then corrected the
  *derivation* from the window itself (1_500 ms) to ``window - transport margin
  - slack`` (1_200 ms): the route's own admission predicate admits at most
  ``window - margin`` (1_300 ms), so advertising the window would have shipped a
  node that refuses its own advertisement at startup.  Both halves cite the
  measurement they are justified by (0.875-1.066 s relayed snapshot round trips;
  the two-Mac peer's 1.83 s / 3.18 s end-to-end cleanup against a 1.5 s window).
  Every other value stays byte-identical; a value that looks wrong is reported,
  not edited.
* **No product imports.**  The module is stdlib-only, so any layer (including
  ``physical_inference_node`` at import time) can bind to it without a cycle.
* **Derived quantities are explicit.**  ``route_cleanup_fanout_window_seconds``
  and ``advertised_maximum_total_cleanup_ms`` are the symbols the composition
  invariants are written in (PLAN D9: the route owns the end-to-end budget; a
  peer's advertised cleanup bound must fit inside the route's fanout window).
* **Invariant checkers return a stable named reason code, never a bare
  ``False``.**  ``None`` means "satisfied"; a non-empty string is the violated
  code.  ``check_composition()`` returns the list of violated codes.
* **Everything existing stays importable and monkeypatchable.**  Consumer
  modules keep their module-level names (``mycelium_live.route.
  _CLEANUP_INLINE_RECEIPT_GRACE_SECONDS`` and friends) as plain attributes
  assigned from this registry, so ``monkeypatch.setattr("<dotted path>", ...)``
  keeps working (see ``tests/live/test_a4_ownership.py``).

Binding status (packet wp2-1 requirement 3): ``CONSUMER_BINDINGS`` lists every
consumer constant that must read this registry.  Constants in modules whose
bytes are hash-pinned by ``contracts/contract-manifest.v1.json`` cannot be
rebound without the canonical manifest pin refresh landing in the same change,
so they were registered here and *test-enforced* by ``tests/testbed/
test_deadline_registry.py`` until a lane that owns ``contracts/**`` does both.
Packets ``wp5-d9`` and ``wp5-bind-residual`` were such lanes: every consumer
that was pending under a contract-pinned module is bound now (see
``BOUND_CONSUMER_BINDINGS``), so ``PENDING_CONTRACT_PINNED_BINDINGS`` is empty.
``PENDING_BINDINGS`` still names the consumers this registry cannot bind alone -
the closed member-runtime bundle and the modules outside the binding packets'
write sets - each with its reason below.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

__all__ = [
    "ARTIFACT_REQUEST_CLOCK_SKEW_MS",
    "AVAILABILITY_RECONCILE_MAX_SECONDS",
    "AVAILABILITY_RECONCILE_MIN_SECONDS",
    "CANCEL_AND_CLEANUP_BUDGET_SECONDS",
    "CLEANUP_FALLBACK_RESERVE_SECONDS",
    "CLEANUP_INLINE_RECEIPT_GRACE_SECONDS",
    "CLEANUP_RECEIPT_POLL_SECONDS",
    "CLEANUP_RECONCILIATION_HORIZON_SECONDS",
    "CLEANUP_RECONCILIATION_WORKERS",
    "CLEANUP_SNAPSHOT_ATTEMPT_SECONDS",
    "CLEANUP_SNAPSHOT_SLICE_SECONDS",
    "COMMAND_DEFAULT_COOPERATIVE_STEP_MS",
    "COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS",
    "COMPOSITION_INVARIANTS",
    "CONSUMER_BINDINGS",
    "PHONE_THERMAL_COOLDOWN_SECONDS",
    "PHONE_REQUEST_EXECUTION_MAX_MS",
    "DEADLINES",
    "DEADLINES_BY_NAME",
    "Deadline",
    "GATEWAY_SESSION_EVICTION_GRACE_SECONDS",
    "GATEWAY_SESSION_SILENT_SUBSCRIBER_STALL_SECONDS",
    "GATEWAY_PRODUCER_STALL_POLL_SECONDS",
    "GATEWAY_SSE_DISCONNECT_POLL_SECONDS",
    "IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS",
    "LIVENESS_ACTIVE_FAILURE_DETECTION_MS",
    "LIVENESS_IDLE_KEEPALIVE_MS",
    "LIVENESS_QUARANTINE_STALE_MS",
    "MEMBER_TRANSPORT_GRANT_TTL_MS",
    "NODE_IDENTITY_CREATE_RETRIES",
    "NODE_IDENTITY_CREATE_RETRY_SECONDS",
    "NODE_COMMAND_TIMEOUT_SECONDS",
    "NODE_LAUNCH_HANDSHAKE_SECONDS",
    "NODE_SESSION_TIMEOUT_SECONDS",
    "NON_BINDING_DEADLINES",
    "PEER_ADVERTISED_CLEANUP_BOUND_SLACK_MS",
    "PEER_ADVERTISED_MAXIMUM_TOTAL_CLEANUP_MS",
    "PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS",
    "PEER_CONNECT_DEADLINE_SECONDS",
    "PENDING_BINDINGS",
    "PENDING_CONTRACT_PINNED_BINDINGS",
    "PENDING_MEMBER_RUNTIME_CLOSURE_BINDINGS",
    "PENDING_OUT_OF_SCOPE_BINDINGS",
    "QUALIFICATION_MAX_AGE_SECONDS",
    "REASON_COOPERATIVE_STEP_EXCEEDS_INTERRUPTION_AND_CLEANUP",
    "REASON_PEER_ADVERTISED_CLEANUP_BOUND_EXCEEDS_ROUTE_FANOUT_WINDOW",
    "REASON_QUALIFICATION_MAX_AGE_NOT_GREATER_THAN_REQUALIFICATION_INTERVAL",
    "REQUALIFICATION_INTERVAL_SECONDS",
    "ROUTE_HEALTH_PROBE_DEADLINE_SECONDS",
    "STAGE_PACK_MAX_AUTHORITY_TTL_MS",
    "STAGE_PACK_MIN_AUTHORITY_TTL_MS",
    "UNITS",
    "advertised_maximum_total_cleanup_ms",
    "check_composition",
    "check_cooperative_step_bound",
    "check_peer_advertised_cleanup_bound",
    "check_qualification_age_bound",
    "deadline",
    "derive_advertised_maximum_total_cleanup_ms",
    "derive_route_cleanup_fanout_window_ms",
    "derive_route_cleanup_fanout_window_seconds",
    "route_cleanup_fanout_window_ms",
    "route_cleanup_fanout_window_seconds",
]

#: Units a :class:`Deadline` may carry.  ``count`` is for bounded attempt /
#: worker / retry budgets that are consumed with the same authority as a clock.
UNITS: Final[frozenset[str]] = frozenset({"seconds", "milliseconds", "count"})


@dataclass(frozen=True, slots=True)
class Deadline:
    """One frozen deadline/budget record.

    ``name``       registry name; ``name.upper()`` is the public constant that
                   carries the same value in this module.
    ``value``      byte-identical to the shipped literal.
    ``unit``       one of :data:`UNITS`.
    ``owner``      module that consumes the value (``path:line``).
    ``derivation`` why the value is what it is, and how it relates to the other
                   records (the composition invariants are written here).
    ``source``     the constant/symbol (or inline literal) this record replaces.
    """

    name: str
    value: float | int
    unit: str
    owner: str
    derivation: str
    source: str

    def __post_init__(self) -> None:
        if self.unit not in UNITS:
            raise ValueError("deadline_unit_unknown")
        if not self.name or not self.derivation or not self.owner or not self.source:
            raise ValueError("deadline_record_incomplete")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError("deadline_value_invalid")
        if self.value <= 0:
            raise ValueError("deadline_value_not_positive")


# --------------------------------------------------------------------------- #
# Values.  One literal per deadline, byte-identical to the shipped constant.
# --------------------------------------------------------------------------- #

#: End-to-end owner budget for cancellation + request-scoped cleanup.  Every
#: other cleanup record below is a partition of this budget, never an extension
#: of it.
#:
#: RE-DERIVED 2026-09-20 by owner decision, from the MEASUREMENT this file had
#: been carrying unbuilt: the two-Mac peer's end-to-end cleanup returns at
#: 1.83 s typical / 3.18 s worst (a13 lane, quoted at
#: PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS below).  The previous 2.0 s total gave
#: a 1.5 s window, which that measurement does not fit -- and because the peer's
#: advertisement was window-derived rather than measured, the admission
#: predicate could never refuse it, so the shortfall surfaced mid-request
#: instead of at qualification (p75r1 and p77r1: command_cleanup_receipt_missing
#: -> failed/request_cleanup_unproven).  PLAN D9's "do not raise the 2 s total"
#: is therefore superseded, deliberately and on the record, by the same
#: measured-not-modelled rule that rebuilt the RSS bound (b7f0e07).
#: Recipe: one recovery slice must clear the measured worst case by 1.15
#:   -> ceil(1.15 x 3.18 s) = 3.7 s, shipped as a 4.0 s slice; the nominal
#:   attempt and the reserve that holds it stay 3.5 s, strictly below the slice
#:   as before (0.5 < 1.1); the inline grace stays 0.5 s; 3.5 + 0.5 + 4.0 = 8.0 s.
#: Composition that follows: window 8.0 - 3.5 = 4.5 s, admitted 4.5 - 0.2 = 4.3 s,
#:   advertised 4.3 - 0.1 = 4.2 s -- which clears the measured worst case by 32 %,
#:   so the advertisement describes the peer AND the predicate regains teeth.
CANCEL_AND_CLEANUP_BUDGET_SECONDS: Final[float] = 8.0

#: The final interval of the budget held back for exactly one bounded
#: request-scoped snapshot fallback; equal to the snapshot attempt by
#: construction.
CLEANUP_FALLBACK_RESERVE_SECONDS: Final[float] = 3.5

#: One bounded request-scoped snapshot round (probe a missing node's cleanup
#: proof) inside the budget.
CLEANUP_SNAPSHOT_ATTEMPT_SECONDS: Final[float] = 3.5

#: The slice handed to one request-scoped fallback snapshot attempt at the point
#: the route resumes it, capped by the owner's immutable deadline.  Measured on
#: the two-Mac route: successful snapshot round trips to the relayed laptop were
#: 0.875-1.066 s, and the previous 0.5 s slice was never even claimed before it
#: expired, so the attempt always missed the window it was meant to recover
#: into.  Re-derived 2026-09-20 from the end-to-end cleanup measurement
#: (1.83 s / 3.18 s): 4.0 s covers the worst case by 1.26 and still fits the
#: budget after the inline receipt grace (0.5 + 4.0 <= 8.0).  A slice shorter
#: than one real cleanup is not a recovery path, it is a second way to miss --
#: which the 1.1 s slice proved to be against a peer that needs 3.18 s.
CLEANUP_SNAPSHOT_SLICE_SECONDS: Final[float] = 4.0

#: One bounded grace for the inline cancellation receipt to arrive before
#: bounded snapshot rounds are allowed to race the delayed response.
CLEANUP_INLINE_RECEIPT_GRACE_SECONDS: Final[float] = 0.5

#: Cadence at which the parent re-reads cleanup receipts, so the node control
#: lane stays available to a concurrently cancelling owner.
CLEANUP_RECEIPT_POLL_SECONDS: Final[float] = 0.1

#: Lifetime of the post-authority cleanup reconciliation scheduler.
CLEANUP_RECONCILIATION_HORIZON_SECONDS: Final[float] = 60.0

#: Worker threads the cleanup reconciliation scheduler may use.
CLEANUP_RECONCILIATION_WORKERS: Final[int] = 4

#: Bound on one idle-keepalive probe of a node whose liveness is being
#: re-observed.
IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS: Final[float] = 0.5

#: Ceiling a command plan may request for interruption + cleanup.
#: The same authority as ``cancel_and_cleanup_budget_seconds``, expressed in ms for
#: the node protocol -- DERIVED, never re-typed (2026-09-20 re-derivation, ERRATA-20:
#: the node's command cap, the controller's ceiling and the qualification document
#: all carry this one window, so a second literal here is a mid-request failure).
COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS: Final[int] = int(
    CANCEL_AND_CLEANUP_BUDGET_SECONDS * 1_000
)

#: Cadence at which the SSE listener wakes to notice a client disconnect while
#: awaiting the next buffered event.
GATEWAY_SSE_DISCONNECT_POLL_SECONDS: Final[float] = 0.1

#: Cadence at which a buffer-blocked producer re-checks the silent-subscriber
#: stall budget before parking again.
GATEWAY_PRODUCER_STALL_POLL_SECONDS: Final[float] = 0.05

#: Bound on one health probe issued by the route's active liveness sweep.
ROUTE_HEALTH_PROBE_DEADLINE_SECONDS: Final[float] = 0.5

#: Cooperative step a command plan gets by default.
COMMAND_DEFAULT_COOPERATIVE_STEP_MS: Final[int] = 100

#: Grace covering the submit->subscribe window of a legitimate fresh request
#: before an abandoned session may be evicted.
GATEWAY_SESSION_EVICTION_GRACE_SECONDS: Final[float] = 0.05

#: Backstop a buffer-blocked producer grants an attached but non-acknowledging
#: consumer before the slot is reclaimed.
GATEWAY_SESSION_SILENT_SUBSCRIBER_STALL_SECONDS: Final[float] = 0.25

#: Liveness: budget in which an active subject must produce an observation.
LIVENESS_ACTIVE_FAILURE_DETECTION_MS: Final[int] = 2_000

#: Liveness: keepalive cadence for an idle subject.
LIVENESS_IDLE_KEEPALIVE_MS: Final[int] = 5_000

#: Liveness: staleness after which a quarantined observation is retired.
LIVENESS_QUARANTINE_STALE_MS: Final[int] = 15_000

#: Member artifact-transport grant TTL.
MEMBER_TRANSPORT_GRANT_TTL_MS: Final[int] = 900_000

#: Availability reconcile: floor of the size-scaled source reconciliation
#: timeout.
AVAILABILITY_RECONCILE_MIN_SECONDS: Final[float] = 30.0

#: Availability reconcile: ceiling of the size-scaled source reconciliation
#: timeout.
AVAILABILITY_RECONCILE_MAX_SECONDS: Final[float] = 1_800.0

#: Launch handshake budget for one physical node process.
NODE_LAUNCH_HANDSHAKE_SECONDS: Final[float] = 2.0

#: Qualification driver: timeout for one command sent to a physical node.
NODE_COMMAND_TIMEOUT_SECONDS: Final[float] = 900.0

#: Qualification driver: timeout for one node control session.  Slightly larger
#: than the per-command timeout so a session outlives its last command.
NODE_SESSION_TIMEOUT_SECONDS: Final[float] = 930.0

#: Identity creation: bounded attempts while a concurrent creator publishes the
#: key file.
NODE_IDENTITY_CREATE_RETRIES: Final[int] = 100

#: Identity creation: sleep between those attempts.
NODE_IDENTITY_CREATE_RETRY_SECONDS: Final[float] = 0.005

#: Accepted clock skew on an assignment-bound artifact request.
ARTIFACT_REQUEST_CLOCK_SKEW_MS: Final[int] = 5_000

#: Stage-pack authority TTL floor: the shortest grant that still covers one
#: measured transfer of the assignment bytes.
STAGE_PACK_MIN_AUTHORITY_TTL_MS: Final[int] = 900_000

#: Stage-pack authority TTL ceiling: a grant above this fails closed
#: (``artifact_transfer_exceeds_maximum_lease``).
STAGE_PACK_MAX_AUTHORITY_TTL_MS: Final[int] = 21_600_000

#: How long a qualification projection may be used before it is stale.  There is
#: no named constant for this today: it is the inline ``3_600_000`` ms literal at
#: ``mycelium_live/route.py:3950`` and ``mycelium_live/router_port.py:217``.
#: This symbol is registry-defined (no runtime consumer invented); binding it is
#: a follow-up for the lane that owns those call sites.
QUALIFICATION_MAX_AGE_SECONDS: Final[float] = 3_600.0

#: How often a live qualification is proactively refreshed, from
#: ``mycelium_live/health.LIVE_QUALIFICATION_REFRESH_AFTER_MS`` (55 minutes).
#: Invariant: ``QUALIFICATION_MAX_AGE_SECONDS > REQUALIFICATION_INTERVAL_SECONDS``
#: - the freshness bound must not expire before the refresher renews it.
REQUALIFICATION_INTERVAL_SECONDS: Final[float] = 3_300.0

#: Packet ``wp2-6`` (new value: no shipped literal is replaced).  How long a
#: **declared connect** waits for a peer's first proof of reachability in a
#: session before it fails with the named reason
#: ``peer_connect_deadline_exceeded`` (``SidecarClient.connect_peer``, or an
#: explicit ``connect_deadline`` on a confirmed send).
#:
#: The authoritative statement of the value is the record's ``derivation``
#: below; the single fact consumers rely on is that it is strictly smaller than
#: ``route_cleanup_fanout_window_seconds``, so one vanished peer can never
#: consume the window the route needs to fan cancellation out to the peers that
#: are still alive.
PEER_CONNECT_DEADLINE_SECONDS: Final[float] = 1.0

#: Command delivery plus receipt return: the interval of the owner's cleanup
#: fanout window that is transport, not cleanup, and that a peer's advertised
#: cleanup bound therefore may not claim.  Measured on the two-Mac route: the
#: relayed peer's end-to-end cleanup returned at 1.83 s / 3.18 s against a
#: 1.5 s window, i.e. it did not fit - so the route's admission predicate
#: (``mycelium_live.route.admit_peer_cleanup_bound``) refuses such a bound by
#: name at startup instead of discovering it mid-request.  200 ms is also the
#: margin the route's own advertised bound keeps below the window.
PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS: Final[int] = 200

#: The headroom the advertised cleanup bound keeps inside the admitted maximum
#: (``window - transport margin``): the a13 lane's measured 1_200 ms bound
#: against the 1_300 ms the route's predicate admits.  Registry-defined
#: composition term: it makes the advertisement strictly admissible by the
#: route's own predicate instead of sitting flush against the boundary.
PEER_ADVERTISED_CLEANUP_BOUND_SLACK_MS: Final[int] = 100


# --------------------------------------------------------------------------- #
# Derived quantities (PLAN 5.2 item 1 / D9).  Declared before the records so a
# record's value can be the derived quantity itself: the composition
# invariants below are written in these symbols, not in copied numbers.
# --------------------------------------------------------------------------- #

def derive_route_cleanup_fanout_window_seconds(
    *,
    budget_seconds: float = CANCEL_AND_CLEANUP_BUDGET_SECONDS,
    fallback_reserve_seconds: float = CLEANUP_FALLBACK_RESERVE_SECONDS,
) -> float:
    """The interval the route actually has to fan cancellation out to peers."""

    return float(budget_seconds) - float(fallback_reserve_seconds)


def derive_route_cleanup_fanout_window_ms(
    *,
    budget_seconds: float = CANCEL_AND_CLEANUP_BUDGET_SECONDS,
    fallback_reserve_seconds: float = CLEANUP_FALLBACK_RESERVE_SECONDS,
) -> int:
    """Same window in milliseconds, exact for whole-millisecond inputs."""

    seconds = derive_route_cleanup_fanout_window_seconds(
        budget_seconds=budget_seconds,
        fallback_reserve_seconds=fallback_reserve_seconds,
    )
    scaled = seconds * 1_000
    if scaled != int(scaled):
        raise ValueError("fanout_window_not_whole_milliseconds")
    return int(scaled)


def derive_advertised_maximum_total_cleanup_ms(
    *,
    fanout_window_ms: float | int | None = None,
    budget_seconds: float = CANCEL_AND_CLEANUP_BUDGET_SECONDS,
    fallback_reserve_seconds: float = CLEANUP_FALLBACK_RESERVE_SECONDS,
    transport_margin_ms: int = PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS,
    slack_ms: int = PEER_ADVERTISED_CLEANUP_BOUND_SLACK_MS,
) -> int:
    """The cleanup bound a peer may advertise: window - margin - slack.

    PLAN D9 requires ``advertised <= route_cleanup_fanout_window_ms``, and the
    route's admission predicate
    (``mycelium_live.route.admit_peer_cleanup_bound``) requires the tighter
    ``advertised <= window - transport margin``.  Deriving the bound as the
    window itself therefore ships a node that refuses its own advertisement at
    startup (1_500 ms > 1_300 ms admitted), which is why the derivation moves
    the *reason* - not just the number - here: 2_000 ms budget - 500 ms fallback
    reserve = 1_500 ms window, - 200 ms transport margin = 1_300 ms admitted,
    - 100 ms slack = 1_200 ms advertised.  Every term is a registry record, so
    one owner decides the reserve, the margin, the slack and the bound together
    and the predicate and the advertisement cannot drift apart.  The window and
    the margin / slack inputs stay overridable for the drift property tests.
    """

    if fanout_window_ms is None:
        window_ms = derive_route_cleanup_fanout_window_ms(
            budget_seconds=budget_seconds,
            fallback_reserve_seconds=fallback_reserve_seconds,
        )
    else:
        if isinstance(fanout_window_ms, bool) or not isinstance(
            fanout_window_ms, (int, float)
        ):
            raise ValueError("invalid_composition_value|fanout_window_ms")
        value = float(fanout_window_ms)
        if value <= 0 or value != int(value):
            raise ValueError("advertised_cleanup_bound_not_whole_milliseconds")
        window_ms = int(value)
    for label, candidate in (
        ("transport_margin_ms", transport_margin_ms),
        ("slack_ms", slack_ms),
    ):
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            raise ValueError(f"invalid_composition_value|{label}")
        if candidate < 0:
            raise ValueError(f"invalid_composition_value|{label}")
    advertised = window_ms - int(transport_margin_ms) - int(slack_ms)
    if advertised <= 0:
        raise ValueError("advertised_cleanup_bound_not_positive")
    return advertised


#: The route's cleanup fanout window: 2.0 s budget - 0.5 s fallback reserve.
route_cleanup_fanout_window_seconds: Final[float] = (
    derive_route_cleanup_fanout_window_seconds()
)

#: Same window in milliseconds (1500 ms today).
route_cleanup_fanout_window_ms: Final[int] = derive_route_cleanup_fanout_window_ms()

#: What a physical node advertises as its total cleanup bound (packet wp5-d9 /
#: PLAN D9, corrected by merge a13-resolve).  Derived, never a second literal:
#: the shipped 2_000 ms literal exceeded the window and produced the round-50
#: shape (the request stalls past the budget and the route has no time left to
#: fan cancellation out), and the window itself (1_500 ms) sits above the
#: 1_300 ms the route's admission predicate admits.  The a13 lane measured the
#: honest side of that question - the two-Mac peer's end-to-end cleanup was
#: 1.83 s / 3.18 s against a 1.5 s window, and successful relayed snapshot round
#: trips were 0.875-1.066 s - so the shipped advertised bound is the admitted
#: maximum less one measured-safe slack, and both the predicate that admits it
#: and this derivation read the same registry records.
PEER_ADVERTISED_MAXIMUM_TOTAL_CLEANUP_MS: Final[int] = (
    derive_advertised_maximum_total_cleanup_ms()
)

#: The same value under the name the node's payload and the gate probe read.
advertised_maximum_total_cleanup_ms: Final[int] = (
    PEER_ADVERTISED_MAXIMUM_TOTAL_CLEANUP_MS
)


# Route-local safety exclusion after an authenticated phone thermal refusal.
PHONE_THERMAL_COOLDOWN_SECONDS: Final[float] = 300.0
# Phone wire translation preserves the host subject and separately bounds local execution.
PHONE_REQUEST_EXECUTION_MAX_MS: Final[int] = 3_600_000

DEADLINES: Final[tuple[Deadline, ...]] = (
    Deadline(
        name="phone_request_execution_max_ms", value=PHONE_REQUEST_EXECUTION_MAX_MS,
        unit="milliseconds", owner="mycelium_live/phone_stage_session.py",
        derivation="At most the existing one-hour command lifetime; remaining host monotonic budget maps once at the phone.",
        source="mycelium_live.phone_stage_session.PHONE_REQUEST_EXECUTION_MAX_MS",
    ),
    Deadline(
        name="phone_thermal_cooldown_seconds", value=PHONE_THERMAL_COOLDOWN_SECONDS,
        unit="seconds", owner="mycelium_router/entry.py",
        derivation="Exclude the hot phone for five minutes; expiry permits retry, never overrides device thermal refusal.",
        source="mycelium_router.entry.PHONE_THERMAL_COOLDOWN_SECONDS",
    ),
    Deadline(
        name="cancel_and_cleanup_budget_seconds",
        value=CANCEL_AND_CLEANUP_BUDGET_SECONDS,
        unit="seconds",
        owner="mycelium_request_gateway/service.py:33,389,392,684",
        derivation=(
            "End-to-end owner budget for cancellation plus request-scoped "
            "cleanup. The gateway stamps it at cancellation start and reports "
            "cancellation_within_bound against it. It partitions, and is never "
            "extended by, the cleanup records below: route fanout window = "
            "cancel_and_cleanup_budget_seconds - cleanup_fallback_reserve_seconds."
        ),
        source="mycelium_request_gateway.service._MAX_CANCELLATION_AND_CLEANUP_SECONDS",
    ),
    Deadline(
        name="cleanup_fallback_reserve_seconds",
        value=CLEANUP_FALLBACK_RESERVE_SECONDS,
        unit="seconds",
        owner="mycelium_live/route.py:82,3257,3258",
        derivation=(
            "Final interval of the budget held back so a complete response "
            "delayed in the control transport still leaves authority for one "
            "bounded request-scoped snapshot fallback. Equal to "
            "cleanup_snapshot_attempt_seconds by construction (the reserve *is* "
            "one attempt, so the budget is never over-committed); it is subtracted "
            "from the budget to form the fanout window, and it is the number the "
            "route applies. It is deliberately NOT the duration an attempt is "
            "handed - that is cleanup_snapshot_slice_seconds (1.1 s, measured) - "
            "and the two numbers must not be collapsed into one another."
        ),
        source="mycelium_live.route._CLEANUP_FALLBACK_RESERVE_SECONDS",
    ),
    Deadline(
        name="cleanup_snapshot_attempt_seconds",
        value=CLEANUP_SNAPSHOT_ATTEMPT_SECONDS,
        unit="seconds",
        owner=(
            "mycelium_deadlines.py (composition term; the route applies it as "
            "cleanup_fallback_reserve_seconds, and this record has no "
            "module-level consumer symbol of its own - see NON_BINDING_DEADLINES)"
        ),
        derivation=(
            "One bounded request-scoped snapshot round against a node whose "
            "cleanup proof is still missing, i.e. the interval the budget holds "
            "back for exactly one attempt: this record and "
            "cleanup_fallback_reserve_seconds are the same number by "
            "construction (the reserve *is* one attempt), and the route applies "
            "it under that name (fanout_deadline = deadline - reserve).  It is "
            "NOT the duration a resumed attempt is handed: that is "
            "cleanup_snapshot_slice_seconds (1.1 s, measured against real "
            "relayed round trips), and the two numbers must not be collapsed "
            "into one another - the 0.5 s slice this record used to describe was "
            "never even claimed before it expired on the relayed path.  Packet "
            "wp5-cleanup-levers removed the route's bound-but-unread "
            "`_CLEANUP_SNAPSHOT_ATTEMPT_SECONDS` copy for exactly that reason, "
            "so this record now has no module-level consumer symbol of its own "
            "and is listed in NON_BINDING_DEADLINES."
        ),
        source=(
            "registry-defined composition term (wp5-cleanup-levers; the applied "
            "symbol is mycelium_live.route._CLEANUP_FALLBACK_RESERVE_SECONDS, "
            "and the handed-out duration is mycelium_live.route."
            "_CLEANUP_SNAPSHOT_SLICE_SECONDS)"
        ),
    ),
    Deadline(
        name="cleanup_snapshot_slice_seconds",
        value=CLEANUP_SNAPSHOT_SLICE_SECONDS,
        unit="seconds",
        owner="mycelium_live/route.py:99,2834",
        derivation=(
            "The duration handed to one request-scoped fallback snapshot attempt "
            "at the point the route resumes it (after the inline receipt grace), "
            "capped by the owner's immutable deadline. Measured by the a13 lane "
            "on the two-Mac route: successful snapshot round trips to the "
            "relayed laptop were 0.875-1.066 s and the first fallback attempt's "
            "0.5 s slice was never even claimed before it expired - a slice "
            "shorter than one real round trip is not a recovery path, it is a "
            "second way to miss. 1.1 s covers the measured maximum with margin "
            "and still fits the owner's budget after the inline grace "
            "(0.5 s + 1.1 s <= 2.0 s cancel_and_cleanup_budget_seconds). It is "
            "this record - cleanup_snapshot_slice_seconds, not "
            "cleanup_snapshot_attempt_seconds, the interval "
            "held back - that names the duration actually spent, and the two "
            "numbers must not be collapsed into one another: the held-back "
            "interval (cleanup_fallback_reserve_seconds, sized by "
            "cleanup_snapshot_attempt_seconds) sizes the fanout window, the slice "
            "bounds the work done inside it."
        ),
        source=(
            "mycelium_live.route._CLEANUP_SNAPSHOT_SLICE_SECONDS "
            "(a13 lane; merged and bound to this registry by a13-resolve)"
        ),
    ),
    Deadline(
        name="cleanup_inline_receipt_grace_seconds",
        value=CLEANUP_INLINE_RECEIPT_GRACE_SECONDS,
        unit="seconds",
        owner="mycelium_live/route.py:83,2681",
        derivation=(
            "Grace given to the inline cancellation receipt (fanout and "
            "lifecycle receipt) before bounded snapshots may race a delayed "
            "response. Bounded so one delayed leaf cannot monopolize the whole "
            "budget; monotonic receipt storage makes the race safe."
        ),
        source="mycelium_live.route._CLEANUP_INLINE_RECEIPT_GRACE_SECONDS",
    ),
    Deadline(
        name="cleanup_receipt_poll_seconds",
        value=CLEANUP_RECEIPT_POLL_SECONDS,
        unit="seconds",
        owner="mycelium_live/route.py:80,2713,2782,3045",
        derivation=(
            "Cadence at which the parent re-reads cleanup receipts while "
            "waiting, so the node control lane still serves other concurrently "
            "cancelling owners instead of queuing stale proof commands. Must "
            "stay well below cleanup_snapshot_attempt_seconds or a single "
            "attempt cannot complete."
        ),
        source="mycelium_live.route._CLEANUP_RECEIPT_POLL_SECONDS",
    ),
    Deadline(
        name="cleanup_reconciliation_horizon_seconds",
        value=CLEANUP_RECONCILIATION_HORIZON_SECONDS,
        unit="seconds",
        owner="mycelium_live/route.py:84,2919",
        derivation=(
            "Lifetime of the post-authority cleanup reconciliation scheduler: "
            "how long the route keeps trying to seal a cleanup receipt after the "
            "request budget (cancel_and_cleanup_budget_seconds) is gone. "
            "Deliberately much larger than the per-request budget because it is "
            "a background retirement bound, not a request-path deadline."
        ),
        source="mycelium_live.route._CLEANUP_RECONCILIATION_HORIZON_SECONDS",
    ),
    Deadline(
        name="cleanup_reconciliation_workers",
        value=CLEANUP_RECONCILIATION_WORKERS,
        unit="count",
        owner="mycelium_live/route.py:85,1080,2963",
        derivation=(
            "Worker threads the cleanup reconciliation scheduler may use. Sized "
            "so a handful of request-scoped reconciliations proceed in parallel "
            "without letting the scheduler starve the shared node control lane."
        ),
        source="mycelium_live.route._CLEANUP_RECONCILIATION_WORKERS",
    ),
    Deadline(
        name="idle_keepalive_probe_deadline_seconds",
        value=IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS,
        unit="seconds",
        owner="mycelium_live/route.py:79,2173",
        derivation=(
            "Bound on one keepalive probe of a node that is being re-observed. "
            "Intentionally the shortest control-path probe: a node that cannot "
            "answer it within this interval is treated as unobserved rather than "
            "as a slow-but-alive peer."
        ),
        source="mycelium_live.route._IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS",
    ),
    Deadline(
        name="peer_advertised_maximum_total_cleanup_ms",
        value=PEER_ADVERTISED_MAXIMUM_TOTAL_CLEANUP_MS,
        unit="milliseconds",
        owner="physical_inference_node.py:93,2404",
        derivation=(
            "The total cleanup bound a physical node advertises in its "
            "concurrency/liveness projection. PLAN D9 makes this the validated "
            "side: it must fit inside the route's fanout window "
            "(advertised <= route_cleanup_fanout_window_ms), fail closed at "
            "startup qualification, never mid-request. Packet wp5-d9 closed the "
            "round-50 defect by deriving the value instead of shipping the "
            "2_000 ms literal that exceeded the window; merge a13-resolve then "
            "corrected the derivation from the window itself (1_500 ms) to "
            "window - transport margin - slack, because the a13 lane's "
            "admission predicate (mycelium_live.route."
            "admit_peer_cleanup_bound) admits at most window - margin: 2_000 ms "
            "cancel_and_cleanup_budget_seconds - 500 ms "
            "cleanup_fallback_reserve_seconds = 1_500 ms window, - 200 ms "
            "peer_cleanup_bound_transport_margin_ms = 1_300 ms admitted, "
            "- 100 ms peer_advertised_cleanup_bound_slack_ms = 1_200 ms "
            "advertised. Advertising the window itself would have shipped a "
            "node its own route refuses at startup (1500 > admitted 1300); the "
            "a13 lane measured the honest side of that boundary - relayed "
            "snapshot round trips 0.875-1.066 s and the two-Mac peer's "
            "end-to-end cleanup 1.83 s / 3.18 s against that 1.5 s window. The "
            "bound is a measured-safe bound, not a restated constant: the "
            "verified round-64 run measured the whole cleanup wait as "
            "``infer_cancel_wait`` 0.000 s on the owner and 0.001 s on the "
            "laptop (runs/a13-macos-demo-export/"
            "round64-c5-met-per-placement-work-on-both-macs.md) - three orders "
            "of magnitude inside the window. Every term of the derivation is a "
            "record in this registry, so one owner decides the reserve, the "
            "margin, the slack and the bound together and the predicate and the "
            "advertisement cannot drift apart."
        ),
        source=(
            "physical_inference_node.MAXIMUM_TOTAL_CLEANUP_MS (a13 lane) and "
            'the payload key "maximum_total_cleanup_ms" - both assigned from '
            "mycelium_deadlines.advertised_maximum_total_cleanup_ms"
        ),
    ),
    Deadline(
        name="peer_cleanup_bound_transport_margin_ms",
        value=PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS,
        unit="milliseconds",
        owner="mycelium_live/route.py:121,161",
        derivation=(
            "Command delivery plus receipt return: the part of the route's "
            "cleanup fanout window that is transport rather than cleanup, and "
            "which a peer's advertised cleanup bound therefore may not claim. "
            "Measured by the a13 lane on the two-Mac route: the relayed peer's "
            "end-to-end cleanup returned at 1.83 s / 3.18 s against the 1.5 s "
            "window, i.e. it does not fit - the exact case "
            "mycelium_live.route.admit_peer_cleanup_bound refuses by name at "
            "startup instead of discovering mid-request. It is also the term "
            "that keeps the shipped advertisement (window - margin - slack = "
            "1_200 ms) strictly inside the admitted maximum (window - margin = "
            "1_300 ms) the same predicate applies to it."
        ),
        source=(
            "mycelium_live.route.PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS "
            "(a13 lane; merged and bound to this registry by a13-resolve)"
        ),
    ),
    Deadline(
        name="peer_advertised_cleanup_bound_slack_ms",
        value=PEER_ADVERTISED_CLEANUP_BOUND_SLACK_MS,
        unit="milliseconds",
        owner=(
            "mycelium_deadlines.py:403 (derive_advertised_maximum_total_"
            "cleanup_ms; registry-internal, no runtime consumer)"
        ),
        derivation=(
            "The headroom the advertised cleanup bound keeps inside the admitted "
            "maximum: advertised = window - transport margin - slack = "
            "1_500 ms - 200 ms - 100 ms = 1_200 ms, against the 1_300 ms "
            "mycelium_live.route.admit_peer_cleanup_bound admits. Registry-"
            "defined composition term (no runtime consumer), recorded rather "
            "than left as an inline literal inside the derivation so this "
            "number has one owner like every other term of the composition. It "
            "is what makes the shipped advertisement strictly admissible by the "
            "route's own predicate: the advertisement and the admission cannot "
            "sit flush against a boundary the other side computes differently."
        ),
        source=(
            "registry-defined (merge a13-resolve; the a13 lane's measured "
            "1_200 ms bound leaves exactly this headroom below the predicate's "
            "admitted 1_300 ms)"
        ),
    ),
    Deadline(
        name="command_interruption_and_cleanup_max_ms",
        value=COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS,
        unit="milliseconds",
        owner="mycelium_live/command_controller.py:19,293,321",
        derivation=(
            "Ceiling a command plan may request for interruption + cleanup; the "
            "controller refuses a larger interruption_and_cleanup_ms. Same "
            "authority as cancel_and_cleanup_budget_seconds, and DERIVED from it "
            "(2026-09-20 re-derivation, ERRATA-20), expressed in ms for "
            "the node protocol. Invariant: cooperative step <= interruption + "
            "cleanup."
        ),
        source="mycelium_live.command_controller.MAX_INTERRUPTION_AND_CLEANUP_MS",
    ),
    Deadline(
        name="gateway_sse_disconnect_poll_seconds",
        value=GATEWAY_SSE_DISCONNECT_POLL_SECONDS,
        unit="seconds",
        owner="mycelium_request_gateway/asgi.py:321",
        derivation=(
            "SSE listener wake-up cadence: the stream coroutine polls the "
            "subscription every 0.1 s so a client disconnect is noticed without "
            "blocking the event loop. Registry-defined (the call site is an "
            "inline literal, not a named constant); it must stay below the "
            "gateway's session stall bound so a stalled consumer is observed."
        ),
        source="inline literal mycelium_request_gateway/asgi.py:321 (no named constant)",
    ),
    Deadline(
        name="gateway_producer_stall_poll_seconds",
        value=GATEWAY_PRODUCER_STALL_POLL_SECONDS,
        unit="seconds",
        owner="mycelium_request_gateway/service.py:630",
        derivation=(
            "Cadence at which a buffer-blocked producer re-checks the "
            "silent-subscriber stall budget before parking again, so a consumer "
            "that starts acking is picked up promptly. Registry-defined (the "
            "call site is an inline literal, not a named constant)."
        ),
        source="inline literal mycelium_request_gateway/service.py:630 (no named constant)",
    ),
    Deadline(
        name="route_health_probe_deadline_seconds",
        value=ROUTE_HEALTH_PROBE_DEADLINE_SECONDS,
        unit="seconds",
        owner="mycelium_live/route.py:1974",
        derivation=(
            "Bound on one health probe issued by the route's active liveness "
            "sweep. Same value as idle_keepalive_probe_deadline_seconds but a "
            "separate call site with its own inline literal; registered so the "
            "duplicate is visible rather than hidden."
        ),
        source="inline literal mycelium_live/route.py:1974 (no named constant)",
    ),
    Deadline(
        name="command_default_cooperative_step_ms",
        value=COMMAND_DEFAULT_COOPERATIVE_STEP_MS,
        unit="milliseconds",
        owner="mycelium_live/command_controller.py:20,292",
        derivation=(
            "Default cooperative step: the largest slice of work a node promises "
            "to finish before it can honour an interruption. The cooperative "
            "invariant is cooperative_step_ms <= interruption_and_cleanup_ms "
            "(100 ms inside 2000 ms today); it is the step, not the cleanup, "
            "that decides whether interruption can land inside the budget."
        ),
        source="mycelium_live.command_controller.DEFAULT_COOPERATIVE_STEP_MS",
    ),
    Deadline(
        name="gateway_session_eviction_grace_seconds",
        value=GATEWAY_SESSION_EVICTION_GRACE_SECONDS,
        unit="seconds",
        owner="mycelium_request_gateway/service.py:163,936",
        derivation=(
            "Grace covering the submit->subscribe window: before it, a new "
            "submission is rejected (gateway_capacity_exhausted) rather than "
            "cancelling a fresh in-flight request that has not subscribed yet. "
            "Far below the cancellation budget on purpose - it protects a fresh "
            "request, it does not wait for cleanup."
        ),
        source="mycelium_request_gateway.service.RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS",
    ),
    Deadline(
        name="gateway_session_silent_subscriber_stall_seconds",
        value=GATEWAY_SESSION_SILENT_SUBSCRIBER_STALL_SECONDS,
        unit="seconds",
        owner="mycelium_request_gateway/service.py:173,625",
        derivation=(
            "Backstop a buffer-blocked producer grants an attached but "
            "non-acknowledging consumer before the slot is reclaimed. Consulted "
            "only on the full-buffer block, so it never delays the ordinary "
            "streaming path."
        ),
        source="mycelium_request_gateway.service.RequestGatewayService.SESSION_SILENT_SUBSCRIBER_STALL_SECONDS",
    ),
    Deadline(
        name="liveness_active_failure_detection_ms",
        value=LIVENESS_ACTIVE_FAILURE_DETECTION_MS,
        unit="milliseconds",
        owner="mycelium_live/liveness.py:16",
        derivation=(
            "Budget in which an actively probed subject must produce an "
            "observation before it is suspected. The route's "
            "idle_keepalive_probe_deadline_seconds (500 ms) sits inside it, so "
            "the prober retires before the detector declares a failure."
        ),
        source="mycelium_live.liveness.ACTIVE_FAILURE_DETECTION_MS",
    ),
    Deadline(
        name="liveness_idle_keepalive_ms",
        value=LIVENESS_IDLE_KEEPALIVE_MS,
        unit="milliseconds",
        owner="mycelium_live/liveness.py:17",
        derivation=(
            "Keepalive cadence for an idle subject: the interval at which the "
            "detector expects traffic to be generated for a subject with no "
            "request-scoped work."
        ),
        source="mycelium_live.liveness.IDLE_KEEPALIVE_MS",
    ),
    Deadline(
        name="liveness_quarantine_stale_ms",
        value=LIVENESS_QUARANTINE_STALE_MS,
        unit="milliseconds",
        owner="mycelium_live/liveness.py:20",
        derivation=(
            "Staleness at which a quarantined observation is retired. Larger "
            "than the active detection bound so quarantine outlives a transient "
            "failure without killing the loop (PLAN 5.3 liveness rules)."
        ),
        source="mycelium_live.liveness.QUARANTINE_STALE_MS",
    ),
    Deadline(
        name="member_transport_grant_ttl_ms",
        value=MEMBER_TRANSPORT_GRANT_TTL_MS,
        unit="milliseconds",
        owner="mycelium_live/member_transport.py:77,1007",
        derivation=(
            "Grant TTL for a member artifact-transport plan, clamped against the "
            "frozen manifest expiry. Must stay >= the stage-pack authority TTL "
            "floor it authorises work under (both 15 minutes today)."
        ),
        source="mycelium_live.member_transport._GRANT_TTL_MS",
    ),
    Deadline(
        name="availability_reconcile_min_seconds",
        value=AVAILABILITY_RECONCILE_MIN_SECONDS,
        unit="seconds",
        owner="mycelium_live/member_transport.py:78,344,345",
        derivation=(
            "Floor of the size-scaled source reconciliation timeout: even a tiny "
            "assignment gets a full 30 s to be reverified. Also the intercept of "
            "the size scaling, so the timeout is monotone in assignment bytes."
        ),
        source="mycelium_live.member_transport._AVAILABILITY_RECONCILE_MIN_SECONDS",
    ),
    Deadline(
        name="availability_reconcile_max_seconds",
        value=AVAILABILITY_RECONCILE_MAX_SECONDS,
        unit="seconds",
        owner="mycelium_live/member_transport.py:79,342",
        derivation=(
            "Ceiling of the size-scaled source reconciliation timeout (30 min). "
            "Must exceed availability_reconcile_min_seconds; today the ceiling "
            "is 60x the floor, so the scaling window is meaningful."
        ),
        source="mycelium_live.member_transport._AVAILABILITY_RECONCILE_MAX_SECONDS",
    ),
    Deadline(
        name="node_launch_handshake_seconds",
        value=NODE_LAUNCH_HANDSHAKE_SECONDS,
        unit="seconds",
        owner="mycelium_node/process.py:52,1486",
        derivation=(
            "Launch handshake budget for one physical node process (ready "
            "handshake, release, exec status). Bounded so a wedged child fails "
            "closed as node_process_start_failed instead of hanging the caller."
        ),
        source="mycelium_node.process._LAUNCH_HANDSHAKE_SECONDS",
    ),
    Deadline(
        name="node_command_timeout_seconds",
        value=NODE_COMMAND_TIMEOUT_SECONDS,
        unit="seconds",
        owner=(
            "physical_inference_qualification.py:77,1816,2069, "
            "mycelium_live/route.py:1722"
        ),
        derivation=(
            "Timeout for one command sent to a physical node by the "
            "qualification driver; the live route reuses the same symbol when "
            "it drives node commands. Orders of magnitude above the request "
            "budget on purpose: it bounds a harness-level command, not a "
            "user-visible request."
        ),
        source="physical_inference_qualification.NODE_COMMAND_TIMEOUT_SECONDS",
    ),
    Deadline(
        name="node_session_timeout_seconds",
        value=NODE_SESSION_TIMEOUT_SECONDS,
        unit="seconds",
        owner=(
            "physical_inference_qualification.py:78,1825,2079, "
            "mycelium_live/route.py:1775"
        ),
        derivation=(
            "Timeout for one node control session; must exceed "
            "node_command_timeout_seconds so the session outlives its last "
            "command (930 s vs 900 s today, a 30 s margin)."
        ),
        source="physical_inference_qualification.NODE_SESSION_TIMEOUT_SECONDS",
    ),
    Deadline(
        name="node_identity_create_retries",
        value=NODE_IDENTITY_CREATE_RETRIES,
        unit="count",
        owner="mycelium_node/identity.py:23,272",
        derivation=(
            "Bounded attempts while a concurrent creator publishes the identity "
            "key file. Worst-case wait is retries x retry_seconds = 0.5 s, which "
            "is deliberately inside node_launch_handshake_seconds."
        ),
        source="mycelium_node.identity._CREATE_RETRIES",
    ),
    Deadline(
        name="node_identity_create_retry_seconds",
        value=NODE_IDENTITY_CREATE_RETRY_SECONDS,
        unit="seconds",
        owner="mycelium_node/identity.py:24,280",
        derivation=(
            "Sleep between identity-creation attempts. Tiny on purpose: identity "
            "creation is a local atomic file race, not a network operation."
        ),
        source="mycelium_node.identity._CREATE_RETRY_SECONDS",
    ),
    Deadline(
        name="artifact_request_clock_skew_ms",
        value=ARTIFACT_REQUEST_CLOCK_SKEW_MS,
        unit="milliseconds",
        owner="mycelium_live/artifact_transport.py:42,547",
        derivation=(
            "Accepted clock skew on an assignment-bound artifact request "
            "(issued-at clamp). Bounded so a stale request cannot be replayed "
            "indefinitely, while tolerating host clock disagreement."
        ),
        source="mycelium_live.artifact_transport._REQUEST_CLOCK_SKEW_MS",
    ),
    Deadline(
        name="stage_pack_min_authority_ttl_ms",
        value=STAGE_PACK_MIN_AUTHORITY_TTL_MS,
        unit="milliseconds",
        owner="mycelium_live/local_preparer.py:41,1148,1149,1304,1443",
        derivation=(
            "Stage-pack authority TTL floor: the shortest grant that still "
            "covers one measured transfer of the assignment bytes. Equal to "
            "member_transport_grant_ttl_ms by construction (15 minutes)."
        ),
        source="mycelium_live.local_preparer._STAGE_PACK_MIN_AUTHORITY_TTL_MS",
    ),
    Deadline(
        name="stage_pack_max_authority_ttl_ms",
        value=STAGE_PACK_MAX_AUTHORITY_TTL_MS,
        unit="milliseconds",
        owner="mycelium_live/local_preparer.py:42,1151",
        derivation=(
            "Stage-pack authority TTL ceiling (6 hours). A size-scaled TTL above "
            "it fails closed with artifact_transfer_exceeds_maximum_lease, so "
            "the scaling can never mint an unbounded lease."
        ),
        source="mycelium_live.local_preparer._STAGE_PACK_MAX_AUTHORITY_TTL_MS",
    ),
    Deadline(
        name="qualification_max_age_seconds",
        value=QUALIFICATION_MAX_AGE_SECONDS,
        unit="seconds",
        owner="mycelium_live/route.py:3950, mycelium_live/router_port.py:217",
        derivation=(
            "Registry-defined symbol: the freshness bound of a qualification "
            "projection (today the inline 3_600_000 ms literal at route.py:3950 "
            "and router_port.py:217 claim an absolute deadline / browser "
            "freshness one hour ahead). No runtime consumer is invented here; "
            "the invariant qualification_max_age_seconds > "
            "requalification_interval_seconds is what makes the pair meaningful."
        ),
        source="registry-defined (no named constant today; inline 3_600_000 ms literals)",
    ),
    Deadline(
        name="requalification_interval_seconds",
        value=REQUALIFICATION_INTERVAL_SECONDS,
        unit="seconds",
        owner="mycelium_live/health.py:11,56, mycelium_live/registry.py:297",
        derivation=(
            "How often a live qualification is proactively refreshed (55 min). "
            "Must be strictly below qualification_max_age_seconds (3600 s) so "
            "the refresher renews the projection before freshness admission "
            "fails closed; today the margin is 300 s."
        ),
        source="mycelium_live.health.LIVE_QUALIFICATION_REFRESH_AFTER_MS",
    ),
    Deadline(
        name="peer_connect_deadline_seconds",
        value=PEER_CONNECT_DEADLINE_SECONDS,
        unit="seconds",
        owner=(
            "mycelium_iroh_sidecar/client.py:28,473,614 (and the Rust dial bound "
            "PEER_CONNECT_TIMEOUT in native/iroh_transport/src/sidecar.rs)"
        ),
        derivation=(
            "Bound on a declared peer connect: how long "
            "mycelium_iroh_sidecar.client.SidecarClient.connect_peer (or a "
            "confirmed send given an explicit connect_deadline) waits for a "
            "peer that has not yet proved reachable in this session before "
            "failing with the named reason "
            "peer_connect_deadline_exceeded. RCA wp2-iroh-delivery defect 2: "
            "iroh 1.0.2 has no connect timeout, so a vanished peer used to "
            "consume the hop's whole latency budget (the 5 s Python delivery "
            "deadline, or the sidecar's 60 s STREAM_IO_TIMEOUT) and surface "
            "only as delivery_deadline_exceeded, with no transport cause. "
            "1.0 s is two thirds of the route's cleanup fanout window: "
            "1.5 s = cancel_and_cleanup_budget_seconds (2.0 s) - "
            "cleanup_fallback_reserve_seconds (0.5 s). It is STRICTLY SMALLER "
            "than that window by construction, which is the invariant this "
            "record exists for: one dead peer must never eat the window the "
            "route needs to fan cancellation out to the peers that are still "
            "alive; the remaining 0.5 s of the window is what the inline "
            "receipt grace and the single bounded snapshot attempt "
            "(cleanup_snapshot_attempt_seconds = 0.5 s) are sized against. It "
            "is also 5x smaller than the shipped Python delivery deadline "
            "(mycelium_router/transports/iroh.py delivery_timeout_seconds = "
            "5.0 s) and 60x smaller than the sidecar STREAM_IO_TIMEOUT, so a "
            "dead peer fails before either budget expires instead of after. "
            "Scope: only a DECLARED connect bounds anything, and only until the "
            "first proof per (endpoint_id, generation) - a peer that already "
            "answered keeps the caller's whole delivery budget, a send that "
            "declares nothing keeps the shipped TimeoutError semantics "
            "byte-for-byte, and a send whose caller budget is tighter keeps the "
            "caller's expiry. That split is deliberate: the neighbouring "
            "regression suite proved that bounding every first *delivery* by "
            "this value renames legitimate slow first deliveries (a 16 MiB "
            "confirmed frame, a peer that answers only after it is configured), "
            "so the deadline is opt-in per connect instead. Adoption: the lane "
            "that owns mycelium_router/transports/iroh.py should call "
            "connect_peer (or pass connect_deadline) for a hop's first frame at "
            "iroh.py:1325; that file is outside packet wp2-6's write set. Open "
            "risk recorded in wp2-6: a cold "
            "cross-network relay dial can legitimately exceed 1.0 s, so the "
            "different-network trial (PLAN exit criterion 4) must decide by "
            "measurement whether this value is raised - within the fanout "
            "window - rather than by guessing here."
        ),
        source=(
            "registry-defined, wp2-6 (no shipped literal replaced; consumer "
            "mycelium_iroh_sidecar.client.PEER_CONNECT_DEADLINE_SECONDS)"
        ),
    ),
)

DEADLINES_BY_NAME: Final[Mapping[str, Deadline]] = MappingProxyType(
    {record.name: record for record in DEADLINES}
)


def deadline(name: str) -> Deadline:
    """Return the frozen record for ``name``; fail closed if unknown."""

    record = DEADLINES_BY_NAME.get(name)
    if record is None:
        raise KeyError("unknown_deadline")
    return record


# (Derived quantities are declared above the records, next to the constants
# they are derived from.)


# --------------------------------------------------------------------------- #
# Composition invariants.  Each checker returns a stable named reason code (or
# None when satisfied); never a bare False.
# --------------------------------------------------------------------------- #

#: PLAN D9, round-50 defect: the peer advertises a cleanup bound the route's
#: fanout window cannot contain (2000 ms advertised vs 1500 ms window today).
REASON_PEER_ADVERTISED_CLEANUP_BOUND_EXCEEDS_ROUTE_FANOUT_WINDOW: Final[str] = (
    "peer_advertised_cleanup_bound_exceeds_route_fanout_window"
)

#: A cooperative step larger than the interruption+cleanup budget can never be
#: interrupted inside the budget.
REASON_COOPERATIVE_STEP_EXCEEDS_INTERRUPTION_AND_CLEANUP: Final[str] = (
    "cooperative_step_exceeds_interruption_and_cleanup"
)

#: A qualification that goes stale before it is refreshed cannot be refreshed.
REASON_QUALIFICATION_MAX_AGE_NOT_GREATER_THAN_REQUALIFICATION_INTERVAL: Final[str] = (
    "qualification_max_age_not_greater_than_requalification_interval"
)

COMPOSITION_INVARIANTS: Final[tuple[str, ...]] = (
    REASON_PEER_ADVERTISED_CLEANUP_BOUND_EXCEEDS_ROUTE_FANOUT_WINDOW,
    REASON_COOPERATIVE_STEP_EXCEEDS_INTERRUPTION_AND_CLEANUP,
    REASON_QUALIFICATION_MAX_AGE_NOT_GREATER_THAN_REQUALIFICATION_INTERVAL,
)


def _number(value: float | int, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid_composition_value|{label}")
    return float(value)


def check_peer_advertised_cleanup_bound(
    *,
    advertised_cleanup_ms: float | int,
    fanout_window_ms: float | int,
) -> str | None:
    """``peer_advertised_cleanup_ms <= route_cleanup_fanout_window_ms``."""

    advertised = _number(advertised_cleanup_ms, "advertised_cleanup_ms")
    window = _number(fanout_window_ms, "fanout_window_ms")
    if advertised > window:
        return REASON_PEER_ADVERTISED_CLEANUP_BOUND_EXCEEDS_ROUTE_FANOUT_WINDOW
    return None


def check_cooperative_step_bound(
    *,
    cooperative_step_ms: float | int,
    interruption_and_cleanup_ms: float | int,
) -> str | None:
    """``cooperative_step_ms <= interruption_ms + cleanup_ms``."""

    step = _number(cooperative_step_ms, "cooperative_step_ms")
    budget = _number(interruption_and_cleanup_ms, "interruption_and_cleanup_ms")
    if step > budget:
        return REASON_COOPERATIVE_STEP_EXCEEDS_INTERRUPTION_AND_CLEANUP
    return None


def check_qualification_age_bound(
    *,
    qualification_max_age_seconds: float | int,
    requalification_interval_seconds: float | int,
) -> str | None:
    """``qualification_max_age_seconds > requalification_interval_seconds``."""

    max_age = _number(qualification_max_age_seconds, "qualification_max_age_seconds")
    interval = _number(
        requalification_interval_seconds, "requalification_interval_seconds"
    )
    if max_age <= interval:
        return (
            REASON_QUALIFICATION_MAX_AGE_NOT_GREATER_THAN_REQUALIFICATION_INTERVAL
        )
    return None


def check_composition(
    *,
    advertised_cleanup_ms: float | int | None = None,
    total_budget_seconds: float | int | None = None,
    fallback_reserve_seconds: float | int | None = None,
    fanout_window_ms: float | int | None = None,
    cooperative_step_ms: float | int | None = None,
    interruption_and_cleanup_ms: float | int | None = None,
    qualification_max_age_seconds: float | int | None = None,
    requalification_interval_seconds: float | int | None = None,
) -> list[str]:
    """Return the violated composition reason codes for one budget composition.

    Every argument defaults to the shipped registry value, so a bare
    ``check_composition()`` answers "does today's shipped composition hold?".
    The fanout window is taken from ``fanout_window_ms`` when given, otherwise
    derived from ``total_budget_seconds`` / ``fallback_reserve_seconds`` - which
    is how a historical composition (e.g. the round-50 preview values) is
    replayed without touching the registry.
    """

    if advertised_cleanup_ms is None:
        advertised_cleanup_ms = advertised_maximum_total_cleanup_ms
    if fanout_window_ms is None:
        budget = (
            CANCEL_AND_CLEANUP_BUDGET_SECONDS
            if total_budget_seconds is None
            else _number(total_budget_seconds, "total_budget_seconds")
        )
        reserve = (
            CLEANUP_FALLBACK_RESERVE_SECONDS
            if fallback_reserve_seconds is None
            else _number(fallback_reserve_seconds, "fallback_reserve_seconds")
        )
        fanout_window_ms = derive_route_cleanup_fanout_window_ms(
            budget_seconds=budget,
            fallback_reserve_seconds=reserve,
        )
    if cooperative_step_ms is None:
        cooperative_step_ms = COMMAND_DEFAULT_COOPERATIVE_STEP_MS
    if interruption_and_cleanup_ms is None:
        interruption_and_cleanup_ms = COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS
    if qualification_max_age_seconds is None:
        qualification_max_age_seconds = QUALIFICATION_MAX_AGE_SECONDS
    if requalification_interval_seconds is None:
        requalification_interval_seconds = REQUALIFICATION_INTERVAL_SECONDS

    verdicts = (
        check_peer_advertised_cleanup_bound(
            advertised_cleanup_ms=advertised_cleanup_ms,
            fanout_window_ms=fanout_window_ms,
        ),
        check_cooperative_step_bound(
            cooperative_step_ms=cooperative_step_ms,
            interruption_and_cleanup_ms=interruption_and_cleanup_ms,
        ),
        check_qualification_age_bound(
            qualification_max_age_seconds=qualification_max_age_seconds,
            requalification_interval_seconds=requalification_interval_seconds,
        ),
    )
    return [code for code in verdicts if code is not None]


# --------------------------------------------------------------------------- #
# Consumer bindings: which module-level constant must read this registry.
# --------------------------------------------------------------------------- #

#: ``(dotted path of the consumer constant, registry name)``.  Every consumer
#: here is assigned from ``mycelium_deadlines.<NAME>`` at its definition site
#: (module level, or the class body for the gateway's session graces), so the
#: registry owns the number; ``tests/testbed/test_deadline_registry.py`` keeps
#: a second copy from reappearing (AST binding check plus an executed-source
#: anti-drift check per consumer).
#:
#: ``wp2-6`` bound ``mycelium_iroh_sidecar/client.py`` to this registry (its
#: ``mycelium_router.wire`` import already makes it a first-party import, so
#: the stdlib-only registry adds no cycle).  ``wp2-1`` bound
#: ``mycelium_live/member_transport.py`` and ``mycelium_node/process.py``.
#: ``wp5-d9`` bound the two ``mycelium_live/route.py`` constants that form the
#: cleanup fanout window.  ``wp5-bind-residual`` bound the route's remaining
#: cleanup/keepalive constants, the gateway's cancellation budget and session
#: graces, and the stage-pack authority TTL floor/ceiling.
#: Merge ``a13-resolve`` bound the a13 lane's measured cleanups - the fallback
#: snapshot slice and the peer-bound transport margin - and the lane's named
#: ``physical_inference_node.MAXIMUM_TOTAL_CLEANUP_MS``, so the imported lane
#: has no second copy of any number this registry owns.
BOUND_CONSUMER_BINDINGS: Final[tuple[tuple[str, str], ...]] = (
    ("mycelium_live.phone_stage_session.PHONE_REQUEST_EXECUTION_MAX_MS", "phone_request_execution_max_ms"),
    ("mycelium_router.entry.PHONE_THERMAL_COOLDOWN_SECONDS", "phone_thermal_cooldown_seconds"),
    (
        "mycelium_iroh_sidecar.client.PEER_CONNECT_DEADLINE_SECONDS",
        "peer_connect_deadline_seconds",
    ),
    (
        "mycelium_live.member_transport._AVAILABILITY_RECONCILE_MAX_SECONDS",
        "availability_reconcile_max_seconds",
    ),
    (
        "mycelium_live.member_transport._AVAILABILITY_RECONCILE_MIN_SECONDS",
        "availability_reconcile_min_seconds",
    ),
    ("mycelium_live.member_transport._GRANT_TTL_MS", "member_transport_grant_ttl_ms"),
    (
        "mycelium_live.local_preparer._STAGE_PACK_MAX_AUTHORITY_TTL_MS",
        "stage_pack_max_authority_ttl_ms",
    ),
    (
        "mycelium_live.local_preparer._STAGE_PACK_MIN_AUTHORITY_TTL_MS",
        "stage_pack_min_authority_ttl_ms",
    ),
    (
        "mycelium_live.route._CLEANUP_FALLBACK_RESERVE_SECONDS",
        "cleanup_fallback_reserve_seconds",
    ),
    (
        "mycelium_live.route._CLEANUP_INLINE_RECEIPT_GRACE_SECONDS",
        "cleanup_inline_receipt_grace_seconds",
    ),
    (
        "mycelium_live.route._CLEANUP_RECEIPT_POLL_SECONDS",
        "cleanup_receipt_poll_seconds",
    ),
    (
        "mycelium_live.route._CLEANUP_RECONCILIATION_HORIZON_SECONDS",
        "cleanup_reconciliation_horizon_seconds",
    ),
    (
        "mycelium_live.route._CLEANUP_RECONCILIATION_WORKERS",
        "cleanup_reconciliation_workers",
    ),
    (
        "mycelium_live.route._CLEANUP_SNAPSHOT_SLICE_SECONDS",
        "cleanup_snapshot_slice_seconds",
    ),
    (
        "mycelium_live.route.PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS",
        "peer_cleanup_bound_transport_margin_ms",
    ),
    (
        "mycelium_live.route._IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS",
        "idle_keepalive_probe_deadline_seconds",
    ),
    (
        "mycelium_node.process._LAUNCH_HANDSHAKE_SECONDS",
        "node_launch_handshake_seconds",
    ),
    (
        "physical_inference_node.MAXIMUM_TOTAL_CLEANUP_MS",
        "peer_advertised_maximum_total_cleanup_ms",
    ),
    (
        "mycelium_request_gateway.service.RequestGatewayService."
        "SESSION_EVICTION_GRACE_SECONDS",
        "gateway_session_eviction_grace_seconds",
    ),
    (
        "mycelium_request_gateway.service.RequestGatewayService."
        "SESSION_SILENT_SUBSCRIBER_STALL_SECONDS",
        "gateway_session_silent_subscriber_stall_seconds",
    ),
    (
        "mycelium_request_gateway.service._MAX_CANCELLATION_AND_CLEANUP_SECONDS",
        "cancel_and_cleanup_budget_seconds",
    ),
    (
        "mycelium_live.route._MAX_CANCELLATION_AND_CLEANUP_SECONDS",
        "cancel_and_cleanup_budget_seconds",
    ),
    (
        "mycelium_live.router_port._MAX_CANCELLATION_AND_CLEANUP_SECONDS",
        "cancel_and_cleanup_budget_seconds",
    ),
    (
        "mycelium_live.command_controller.MAX_INTERRUPTION_AND_CLEANUP_MS",
        "command_interruption_and_cleanup_max_ms",
    ),
)

#: ``(dotted path, registry name)`` for consumers whose module belongs to the
#: **closed member-runtime bundle**
#: (``mycelium_member_runtime.RUNTIME_PATHS``: the least-authority file set that
#: is delivered to a member host and import-probed from a private directory).
#: Binding them would make ``mycelium_live/artifact_transport.py`` and
#: ``mycelium_node/identity.py`` import ``mycelium_deadlines``, which is not in
#: that closure, so the bundle's import probe fails
#: (``MemberRuntimeError("member_runtime_import_probe_failed")``, caught by
#: ``tests/live/test_member_runtime.py``).  Extending the closure is a
#: least-authority decision owned by the lane that owns
#: ``mycelium_member_runtime.py``; the exact patch (closure entry + bindings) is
#: recorded in the wp2-1 run directory.  Until it lands, the values are
#: test-enforced from here.  Packet ``wp5-bind-residual`` left these three rows
#: pending for exactly this reason: the binding cannot land without the closure
#: decision, and ``mycelium_deadlines.py`` is still not a member of the closure.
PENDING_MEMBER_RUNTIME_CLOSURE_BINDINGS: Final[tuple[tuple[str, str], ...]] = (
    (
        "mycelium_live.artifact_transport._REQUEST_CLOCK_SKEW_MS",
        "artifact_request_clock_skew_ms",
    ),
    ("mycelium_node.identity._CREATE_RETRIES", "node_identity_create_retries"),
    (
        "mycelium_node.identity._CREATE_RETRY_SECONDS",
        "node_identity_create_retry_seconds",
    ),
)

#: ``(dotted path, registry name)`` for consumers whose module bytes are
#: hash-pinned by ``contracts/contract-manifest.v1.json``
#: (``mycelium_live/route.py`` 5 specs, ``mycelium_request_gateway/service.py``
#: 2, ``mycelium_live/local_preparer.py`` 1).  Rebinding one is a one-line
#: assignment, but it must land in the same change as a canonical manifest
#: owner-source pin refresh (``scripts/generate_contract_manifest.py``).
#: Packet ``wp5-d9`` owned ``mycelium_live/route.py`` and that refresh for the
#: two constants that form the cleanup fanout window; packet
#: ``wp5-bind-residual`` owned the same for the remaining route constants, the
#: gateway's cancellation budget + session graces, and the stage-pack authority
#: TTLs.  All of them are bound now (``BOUND_CONSUMER_BINDINGS``), so this
#: group is empty - a new entry here means a new contract-pinned consumer was
#: registered and still needs its binding + pin refresh.
PENDING_CONTRACT_PINNED_BINDINGS: Final[tuple[tuple[str, str], ...]] = ()

#: ``(dotted path, registry name)`` for consumers the registry registers and
#: test-enforces but does not rebind: these modules are outside the binding
#: packets' write sets (``mycelium_live/command_controller.py``,
#: ``mycelium_live/liveness.py``, ``mycelium_live/health.py``,
#: ``physical_inference_qualification.py``).  Their values are pinned here by
#: test, so a value edit in any of them fails the suite.  Packet
#: 2026-09-20 (ERRATA-20): ``MAX_INTERRUPTION_AND_CLEANUP_MS`` MOVED OUT of this
#: group into ``BOUND_CONSUMER_BINDINGS`` -- the cleanup-budget re-derivation
#: changed its value, and a test-enforced literal is exactly what cannot carry a
#: change of authority.  Seven rows remain.
#: ``wp5-bind-residual`` left these eight rows pending for lane-ownership
#: reasons: binding them is mechanically possible but out of that packet's
#: named write scope (route.py, request-gateway service, local_preparer).
PENDING_OUT_OF_SCOPE_BINDINGS: Final[tuple[tuple[str, str], ...]] = (
    (
        "mycelium_live.command_controller.DEFAULT_COOPERATIVE_STEP_MS",
        "command_default_cooperative_step_ms",
    ),
    (
        "mycelium_live.health.LIVE_QUALIFICATION_REFRESH_AFTER_MS",
        "requalification_interval_seconds",
    ),
    ("mycelium_live.liveness.ACTIVE_FAILURE_DETECTION_MS", "liveness_active_failure_detection_ms"),
    ("mycelium_live.liveness.IDLE_KEEPALIVE_MS", "liveness_idle_keepalive_ms"),
    ("mycelium_live.liveness.QUARANTINE_STALE_MS", "liveness_quarantine_stale_ms"),
    (
        "physical_inference_qualification.NODE_COMMAND_TIMEOUT_SECONDS",
        "node_command_timeout_seconds",
    ),
    (
        "physical_inference_qualification.NODE_SESSION_TIMEOUT_SECONDS",
        "node_session_timeout_seconds",
    ),
)

#: Every consumer constant the registry single-sources.
CONSUMER_BINDINGS: Final[tuple[tuple[str, str], ...]] = tuple(
    sorted(
        BOUND_CONSUMER_BINDINGS
        + PENDING_MEMBER_RUNTIME_CLOSURE_BINDINGS
        + PENDING_CONTRACT_PINNED_BINDINGS
        + PENDING_OUT_OF_SCOPE_BINDINGS
    )
)

#: Consumers registered but not rebound by this packet, with the reason.
PENDING_BINDINGS: Final[tuple[tuple[str, str], ...]] = tuple(
    sorted(
        PENDING_MEMBER_RUNTIME_CLOSURE_BINDINGS
        + PENDING_CONTRACT_PINNED_BINDINGS
        + PENDING_OUT_OF_SCOPE_BINDINGS
    )
)

#: Records with no named module-level consumer symbol: their value is consumed
#: as an inline literal, as a payload key, or (for
#: ``peer_advertised_cleanup_bound_slack_ms``) by this module's own derivation,
#: so they are guarded by value-level tests (canonical producer pin / AST /
#: literal drift) instead of a constant binding.  Naming those call sites is a
#: follow-up owned by the module that holds them.  The a13 lane's named
#: ``physical_inference_node.MAXIMUM_TOTAL_CLEANUP_MS`` moved
#: ``peer_advertised_maximum_total_cleanup_ms`` out of this group when merge
#: a13-resolve bound it; the executed-payload test in
#: ``tests/testbed/test_deadline_registry.py`` stays its behavioural guard,
#: together with the canonical producer pin.
#:
#: ``cleanup_snapshot_attempt_seconds`` joined this group in packet
#: ``wp5-cleanup-levers``: merge a13-resolve bound it as
#: ``mycelium_live.route._CLEANUP_SNAPSHOT_ATTEMPT_SECONDS`` and consumed it
#: nowhere, while the route's live fallback attempt is handed the *measured*
#: ``cleanup_snapshot_slice_seconds`` (1.1 s) - a bound-but-unread copy is the
#: exact drift the one-owner-per-number discipline exists to prevent, and the
#: two numbers must differ (the interval held back vs the duration handed out).
#: The interval the route applies is ``cleanup_fallback_reserve_seconds``, which
#: this record sizes; ``tests/testbed/test_deadline_registry.py::
#: test_cleanup_snapshot_attempt_record_has_no_consumerless_binding`` keeps the
#: binding from reappearing, and
#: ``tests/live/test_cleanup_bound_contract.py::
#: test_fallback_reserve_still_covers_a_whole_snapshot_attempt`` keeps the
#: reserve-vs-attempt relationship, with the records naming each other.
NON_BINDING_DEADLINES: Final[frozenset[str]] = frozenset(
    {
        "cleanup_snapshot_attempt_seconds",
        "gateway_producer_stall_poll_seconds",
        "gateway_sse_disconnect_poll_seconds",
        "peer_advertised_cleanup_bound_slack_ms",
        "qualification_max_age_seconds",
        "route_health_probe_deadline_seconds",
    }
)
