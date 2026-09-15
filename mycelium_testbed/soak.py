# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seeded control-plane fault-injection soak (packet ``wp2-3``, ``PLAN.md`` 5.2 item 2).

What this module is
-------------------

A **model-free** control-plane soak over the *real* mailbox path
(``enqueue`` / ``poll`` / ``ack`` / ``result``) and the *real* two-node control
path.  It owns no product behaviour and re-implements no harness:

* the canonical request path is driven by ``tests/e2e_request_iroh/harness.py``'s
  own ``run_complete_request`` through ``mycelium_testbed.iroh_e2e`` (the WP2-2
  trace adapter), with only the *test-harness factories*
  (``harness._make_transport``, ``harness._DeterministicStageRuntime``) replaced
  for the duration of a run - the same seam ``iroh_e2e`` already uses;
* the mailbox itself is the production ``RequestGatewayService`` /
  ``EventSubscription`` pair the harness's own ``_service_stack`` builds
  (``submit`` = enqueue, ``next_event`` = poll, ``ack`` = ack, the terminal
  ``StreamEvent`` = result);
* lifecycle traces are ``mycelium_testbed.trace`` run directories, one per seed,
  written outside the repository.

Under a **seeded** RNG it drops, duplicates, delays and reorders mailbox
traffic and restarts the owner's subscription and the peer's process.  Every
decision comes from :class:`FaultSchedule`, a pure function of
``(seed, events, weights, forced)``.  A failure replays from ``--seed`` alone.

The four invariants (stable named codes, ``PLAN.md`` 5.2 item 2)
---------------------------------------------------------------

========================================= ==============================
code                                      meaning
========================================= ==============================
``at_most_one_native_execution_per_command``
                                          one command identity is executed at
                                          most once, whatever the mailbox
                                          delivers
``nonterminal_row_not_retired_within_bound``
                                          a durable row with no terminal frame
                                          is not retired (removed from the
                                          session table) before its bound
``control_loop_death_while_membership_valid``
                                          a control loop does not stop
                                          producing observations while the
                                          membership still calls the subject
                                          valid
``request_without_terminal_frame_within_budget``
                                          every admitted request reaches a
                                          terminal frame (success or *named*
                                          failure) within its budget
========================================= ==============================

**Every bound comes from ``mycelium_deadlines.py``** (packet requirement 2: "do
not re-declare any number in the soak").  :func:`registry_seconds` and
:func:`registry_count` read ``mycelium_deadlines.deadline(name)``, so
:class:`SoakBounds` moves when the registry moves and
``tests/testbed/test_soak_bounds_are_registry_sourced.py`` proves it.  The
mapping:

* ``nonterminal_row_not_retired_within_bound`` ->
  ``cancel_and_cleanup_budget_seconds``: while the owner's end-to-end
  cancellation + request-scoped cleanup budget has not elapsed, the owner is
  still entitled to observe the request's terminal state, so a non-terminal row
  must not disappear;
* ``control_loop_death_while_membership_valid`` ->
  ``liveness_active_failure_detection_ms`` (an actively probed subject must
  produce an observation inside it) and ``liveness_idle_keepalive_ms`` for a
  subject with no request-scoped work;
* ``request_without_terminal_frame_within_budget`` -> the budget of the
  transition that is actually pending for the row: a row that stalled or was
  cancelled is judged against ``cancel_and_cleanup_budget_seconds``, a row the
  driver still holds is judged against ``node_command_timeout_seconds`` (the
  symbol ``mycelium_live/route.py`` reuses when it drives node commands);
* the soak's own cadences (poll window, eviction grace, silent-subscriber
  stall, probe, launch handshake, delay magnitudes) are
  ``gateway_sse_disconnect_poll_seconds``, ``cleanup_receipt_poll_seconds``,
  ``gateway_session_eviction_grace_seconds``,
  ``gateway_session_silent_subscriber_stall_seconds``,
  ``idle_keepalive_probe_deadline_seconds``, ``cleanup_inline_receipt_grace_seconds``
  and ``node_launch_handshake_seconds``.

``at_most_one_native_execution_per_command`` needs no deadline: its bound is the
count ``1``, which is the invariant itself.

Determinism (requirement 1)
---------------------------

``run_digest`` covers only *soak-determined* material: the seed, the event
count, the weight digest, the schedule digest, the bound digest, the forced-fault
list, the fault-application counts and the violations.  Wall-clock and monotonic
stamps, pids, arrival counts and per-command execution counts are recorded in the
per-seed record and the trace (so a failure is diagnosable) but are **excluded
from the digest by design** - those are the things under test, and a digest that
included them could not be reproduced.  ``product_digest`` is the mirror image:
it digests the *observed product* material (execution counts, terminal classes)
and is reported next to the run digest so a same-seed behavioural difference is
visible even though it does not change the replay identity.

Falsification (requirement 3)
-----------------------------

A soak that has never been observed to fail is not evidence (``PLAN.md`` 5.1).
:data:`FALSIFICATION_FAULTS` are therefore never drawn by the random schedule;
they are reachable only through ``--force-fault`` and are asserted by
``tests/testbed/test_soak_falsification.py``:

* ``double_execution`` -> ``at_most_one_native_execution_per_command``
* ``stall_control_loop`` -> ``control_loop_death_while_membership_valid``
* ``retire_nonterminal_row`` -> ``nonterminal_row_not_retired_within_bound``
* ``drop_terminal_frame`` -> ``request_without_terminal_frame_within_budget``

What is deliberately *not* injected
-----------------------------------

A frame-level *drop* or *reorder* of an iroh control frame wedges the harness's
own request driver (its own assertions fire before the product can be judged),
which would produce a harness artifact rather than a product signal.  The soak
therefore injects ``drop`` and ``reorder`` where they have real product
semantics - the mailbox surface - and keeps the transport layer to
``duplicate`` and ``delay`` (both non-destructive, both real on the wire).  All
four fault kinds are injected; the split is stated rather than hidden.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import random
import threading
import time
from enum import StrEnum
from typing import Any, Callable, Iterator, Mapping, Sequence

import mycelium_deadlines as deadlines
from mycelium_testbed import iroh_e2e
from mycelium_testbed.trace import (
    EventKind,
    TraceError,
    TraceRecorder,
    TraceRunDirectory,
    load_run,
    summarise,
)

SOAK_SCHEMA_VERSION = "mycelium.testbed.soak.v1"

#: The four named invariant codes (packet wp2-3 requirement 2).  Stable strings:
#: a rename is a schema change and the tests pin the literals.
INVARIANT_AT_MOST_ONE_NATIVE_EXECUTION_PER_COMMAND = (
    "at_most_one_native_execution_per_command"
)
INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND = (
    "nonterminal_row_not_retired_within_bound"
)
INVARIANT_CONTROL_LOOP_DEATH_WHILE_MEMBERSHIP_VALID = (
    "control_loop_death_while_membership_valid"
)
INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET = (
    "request_without_terminal_frame_within_budget"
)

INVARIANT_CODES: tuple[str, ...] = (
    INVARIANT_AT_MOST_ONE_NATIVE_EXECUTION_PER_COMMAND,
    INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND,
    INVARIANT_CONTROL_LOOP_DEATH_WHILE_MEMBERSHIP_VALID,
    INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET,
)

#: One native execution per command identity is the invariant itself, not a
#: deadline: the only bound the soak states in its own words.
MAXIMUM_NATIVE_EXECUTIONS_PER_COMMAND = 1

DEFAULT_RUNS_DIR = (
    Path(os.environ["MYCELIUM_RUNS_DIR"])
    if "MYCELIUM_RUNS_DIR" in os.environ
    else Path.home() / "Documents" / "mycelium-core-program" / "runs"
)
DEFAULT_RUN_ROOT_NAME = "wp2-3"


class SoakError(RuntimeError):
    """Fail-closed soak error; never a bare ``False``."""


# --------------------------------------------------------------------------- #
# Bounds: read from mycelium_deadlines, never re-declared here.
# --------------------------------------------------------------------------- #


def _registry_record(name: str):
    try:
        return deadlines.deadline(name)
    except KeyError as error:  # pragma: no cover - registry contract guard
        raise SoakError(f"soak_bound_not_in_deadline_registry:{name}") from error


def registry_seconds(name: str) -> float:
    """A registry duration in seconds, converted from the record's own unit."""

    record = _registry_record(name)
    if record.unit == "seconds":
        return float(record.value)
    if record.unit == "milliseconds":
        return float(record.value) / 1_000
    raise SoakError(f"soak_bound_is_not_a_duration:{name}:{record.unit}")


def registry_count(name: str) -> int:
    """A registry count budget (attempts / workers / retries)."""

    record = _registry_record(name)
    if record.unit != "count":
        raise SoakError(f"soak_bound_is_not_a_count:{name}:{record.unit}")
    return int(record.value)


#: Registry names the seeded schedule may draw a delay magnitude from, with the
#: reason the soak may sleep that long.
DELAY_SOURCES: tuple[tuple[str, str], ...] = (
    ("gateway_session_eviction_grace_seconds", "submit->subscribe grace"),
    ("cleanup_receipt_poll_seconds", "route receipt observation grid"),
    ("gateway_sse_disconnect_poll_seconds", "SSE listener wake-up cadence"),
    ("gateway_session_silent_subscriber_stall_seconds", "silent-subscriber backstop"),
    ("idle_keepalive_probe_deadline_seconds", "one liveness probe"),
    ("cleanup_inline_receipt_grace_seconds", "inline receipt grace"),
)


@dataclass(frozen=True)
class SoakBounds:
    """Every soak bound, resolved from ``mycelium_deadlines``."""

    nonterminal_row_retirement_seconds: float
    request_terminal_cancelled_seconds: float
    request_terminal_driver_seconds: float
    control_loop_stall_seconds: float
    control_loop_idle_seconds: float
    row_eviction_grace_seconds: float
    silent_subscriber_stall_seconds: float
    poll_seconds: float
    probe_seconds: float
    launch_handshake_seconds: float
    restart_retry_budget_seconds: float
    delay_choices: tuple[tuple[str, float], ...]

    def limits(self) -> dict[str, Any]:
        return {
            "nonterminal_row_retirement_seconds": self.nonterminal_row_retirement_seconds,
            "request_terminal_cancelled_seconds": self.request_terminal_cancelled_seconds,
            "request_terminal_driver_seconds": self.request_terminal_driver_seconds,
            "control_loop_stall_seconds": self.control_loop_stall_seconds,
            "control_loop_idle_seconds": self.control_loop_idle_seconds,
            "row_eviction_grace_seconds": self.row_eviction_grace_seconds,
            "silent_subscriber_stall_seconds": self.silent_subscriber_stall_seconds,
            "poll_seconds": self.poll_seconds,
            "probe_seconds": self.probe_seconds,
            "launch_handshake_seconds": self.launch_handshake_seconds,
            "restart_retry_budget_seconds": self.restart_retry_budget_seconds,
            "delay_choices": {name: value for name, value in self.delay_choices},
        }

    def digest(self) -> str:
        payload = json.dumps(self.limits(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def soak_bounds() -> SoakBounds:
    """Build the bound set from the deadline registry (its only source)."""

    return SoakBounds(
        nonterminal_row_retirement_seconds=registry_seconds(
            "cancel_and_cleanup_budget_seconds"
        ),
        request_terminal_cancelled_seconds=registry_seconds(
            "cancel_and_cleanup_budget_seconds"
        ),
        request_terminal_driver_seconds=registry_seconds("node_command_timeout_seconds"),
        control_loop_stall_seconds=registry_seconds("liveness_active_failure_detection_ms"),
        control_loop_idle_seconds=registry_seconds("liveness_idle_keepalive_ms"),
        row_eviction_grace_seconds=registry_seconds("gateway_session_eviction_grace_seconds"),
        silent_subscriber_stall_seconds=registry_seconds(
            "gateway_session_silent_subscriber_stall_seconds"
        ),
        poll_seconds=registry_seconds("gateway_sse_disconnect_poll_seconds"),
        probe_seconds=registry_seconds("idle_keepalive_probe_deadline_seconds"),
        launch_handshake_seconds=registry_seconds("node_launch_handshake_seconds"),
        restart_retry_budget_seconds=(
            registry_count("node_identity_create_retries")
            * registry_seconds("node_identity_create_retry_seconds")
        ),
        delay_choices=tuple((name, registry_seconds(name)) for name, _ in DELAY_SOURCES),
    )


# --------------------------------------------------------------------------- #
# Seeded fault schedule
# --------------------------------------------------------------------------- #


class FaultKind(StrEnum):
    NONE = "none"
    DROP = "drop"
    DUPLICATE = "duplicate"
    DELAY = "delay"
    REORDER = "reorder"
    RESTART_OWNER = "restart_owner"
    RESTART_PEER = "restart_peer"
    STALL_CONTROL_LOOP = "stall_control_loop"
    DOUBLE_EXECUTION = "double_execution"
    RETIRE_NONTERMINAL_ROW = "retire_nonterminal_row"
    DROP_TERMINAL_FRAME = "drop_terminal_frame"


class FaultTarget(StrEnum):
    MAILBOX = "mailbox"
    TRANSPORT = "transport"
    RUNTIME = "runtime"
    PROCESS = "process"


#: Faults only the falsification tests may inject; never drawn by the RNG.
FALSIFICATION_FAULTS: tuple[FaultKind, ...] = (
    FaultKind.DOUBLE_EXECUTION,
    FaultKind.STALL_CONTROL_LOOP,
    FaultKind.RETIRE_NONTERMINAL_ROW,
    FaultKind.DROP_TERMINAL_FRAME,
)

#: The faults the random schedule draws, with their target surface.
RANDOM_SCHEDULE_FAULTS: tuple[tuple[FaultKind, FaultTarget], ...] = (
    (FaultKind.NONE, FaultTarget.MAILBOX),
    (FaultKind.DROP, FaultTarget.MAILBOX),
    (FaultKind.DUPLICATE, FaultTarget.MAILBOX),
    (FaultKind.DELAY, FaultTarget.MAILBOX),
    (FaultKind.REORDER, FaultTarget.MAILBOX),
    (FaultKind.RESTART_OWNER, FaultTarget.MAILBOX),
    (FaultKind.RESTART_PEER, FaultTarget.PROCESS),
)

#: Transport-layer faults: non-destructive by design (see the module docstring).
TRANSPORT_SCHEDULE_FAULTS: tuple[FaultKind, ...] = (
    FaultKind.NONE,
    FaultKind.DELAY,
    FaultKind.DUPLICATE,
)


@dataclass(frozen=True)
class FaultWeights:
    """Seeded fault probabilities; ``none`` takes the remainder.

    ``transport_duplicate`` is deliberately small: a duplicated control frame is
    a *real* fault the transport must tolerate, but it is not free - the control
    path fails the request closed with a named failure rather than absorbing the
    redelivery (see the seed records), so a high rate would turn every canonical
    request episode into that failure path and cost the sweep its success-path
    evidence.  ``transport_delay`` stays bounded by the registry's own cadences.
    """

    drop: float = 0.04
    duplicate: float = 0.05
    delay: float = 0.06
    reorder: float = 0.03
    restart_owner: float = 0.02
    restart_peer: float = 0.01
    transport_delay: float = 0.05
    transport_duplicate: float = 0.02

    def as_document(self) -> dict[str, float]:
        return {
            "drop": self.drop,
            "duplicate": self.duplicate,
            "delay": self.delay,
            "reorder": self.reorder,
            "restart_owner": self.restart_owner,
            "restart_peer": self.restart_peer,
            "transport_delay": self.transport_delay,
            "transport_duplicate": self.transport_duplicate,
        }

    def digest(self) -> str:
        payload = json.dumps(self.as_document(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FaultEvent:
    """One seeded decision: a single mailbox event of the soak."""

    index: int
    kind: FaultKind
    target: FaultTarget
    bound_name: str | None
    bound_seconds: float | None

    def as_document(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": str(self.kind),
            "target": str(self.target),
            "bound_name": self.bound_name,
            "bound_seconds": self.bound_seconds,
        }


@dataclass(frozen=True)
class ForcedFault:
    """A fault pinned to a step (falsification), or to the first step."""

    kind: FaultKind
    step: int | None = None

    def as_document(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "step": self.step}


class FaultSchedule:
    """A pure function of ``(seed, events, weights, forced)``: the replay unit."""

    def __init__(
        self,
        *,
        seed: int,
        events: int,
        weights: FaultWeights | None = None,
        forced: Sequence[ForcedFault] = (),
    ) -> None:
        if isinstance(events, bool) or not isinstance(events, int) or events < 0:
            raise SoakError("soak_invalid_event_count")
        self.seed = int(seed)
        self.events = events
        self.weights = weights or FaultWeights()
        self.forced = tuple(forced)
        for fault in self.forced:
            if fault.kind not in FALSIFICATION_FAULTS:
                raise SoakError(
                    f"soak_forced_fault_not_a_falsification_fault:{fault.kind}"
                )
        self._events = self._build()
        self._cursor = 0

    # -- construction ---------------------------------------------------- #

    def _weight_for(self, kind: FaultKind) -> float:
        table = {
            FaultKind.DROP: self.weights.drop,
            FaultKind.DUPLICATE: self.weights.duplicate,
            FaultKind.DELAY: self.weights.delay,
            FaultKind.REORDER: self.weights.reorder,
            FaultKind.RESTART_OWNER: self.weights.restart_owner,
            FaultKind.RESTART_PEER: self.weights.restart_peer,
        }
        return table[kind]

    def _build(self) -> tuple[FaultEvent, ...]:
        rng = random.Random(self.seed)
        choices = RANDOM_SCHEDULE_FAULTS
        weights = [1.0 - sum(self._weight_for(kind) for kind, _ in choices[1:])]
        weights.extend(self._weight_for(kind) for kind, _ in choices[1:])
        if any(weight < 0 for weight in weights):
            raise SoakError("soak_fault_weights_exceed_one")
        forced_by_step: dict[int, FaultKind] = {}
        for fault in self.forced:
            forced_by_step[0 if fault.step is None else int(fault.step)] = fault.kind
        events: list[FaultEvent] = []
        for index in range(self.events):
            forced_kind = forced_by_step.get(index)
            if forced_kind is not None:
                target = {
                    FaultKind.DOUBLE_EXECUTION: FaultTarget.RUNTIME,
                    FaultKind.STALL_CONTROL_LOOP: FaultTarget.RUNTIME,
                    FaultKind.RETIRE_NONTERMINAL_ROW: FaultTarget.MAILBOX,
                    FaultKind.DROP_TERMINAL_FRAME: FaultTarget.MAILBOX,
                }[forced_kind]
                bound_name, bound_seconds = self._forced_bound(forced_kind)
                events.append(
                    FaultEvent(index, forced_kind, target, bound_name, bound_seconds)
                )
                continue
            kind, target = rng.choices(choices, weights=weights, k=1)[0]
            bound_name, bound_seconds = self._random_bound(kind, rng)
            events.append(FaultEvent(index, kind, target, bound_name, bound_seconds))
        return tuple(events)

    @staticmethod
    def _random_bound(
        kind: FaultKind, rng: random.Random
    ) -> tuple[str | None, float | None]:
        if kind is FaultKind.DELAY:
            name, value = rng.choice(soak_bounds().delay_choices)
            return name, value
        return None, None

    @staticmethod
    def _forced_bound(kind: FaultKind) -> tuple[str | None, float | None]:
        bounds = soak_bounds()
        if kind is FaultKind.STALL_CONTROL_LOOP:
            # Held past the registry's own active-failure detection bound plus one
            # idle keepalive, so the judged window is unambiguously outside it.
            return (
                "liveness_active_failure_detection_ms",
                bounds.control_loop_stall_seconds + bounds.control_loop_idle_seconds,
            )
        if kind is FaultKind.RETIRE_NONTERMINAL_ROW:
            return (
                "cancel_and_cleanup_budget_seconds",
                bounds.nonterminal_row_retirement_seconds,
            )
        if kind is FaultKind.DROP_TERMINAL_FRAME:
            return (
                "cancel_and_cleanup_budget_seconds",
                bounds.request_terminal_cancelled_seconds,
            )
        return None, None

    # -- consumption ----------------------------------------------------- #

    @property
    def consumed(self) -> int:
        return self._cursor

    def next_fault(self) -> FaultEvent:
        if self._cursor >= len(self._events):
            raise SoakError("soak_schedule_exhausted")
        event = self._events[self._cursor]
        self._cursor += 1
        return event

    def events_for(self, path: str) -> tuple[FaultEvent, ...]:
        """The schedule slice one path consumes (``mailbox`` or ``transport``)."""

        if path == "mailbox":
            return self._events
        if path == "transport":
            bounds = soak_bounds()
            rng = random.Random(((self.seed + 1) * 0x9E3779B1) & 0xFFFFFFFF)
            plan: list[FaultEvent] = []
            for index in range(self.events):
                weights = [
                    1.0 - (self.weights.transport_delay + self.weights.transport_duplicate),
                    self.weights.transport_delay,
                    self.weights.transport_duplicate,
                ]
                kind = rng.choices(TRANSPORT_SCHEDULE_FAULTS, weights=weights, k=1)[0]
                if kind is FaultKind.DELAY:
                    name, value = rng.choice(bounds.delay_choices)
                    plan.append(
                        FaultEvent(index, kind, FaultTarget.TRANSPORT, name, value)
                    )
                else:
                    plan.append(
                        FaultEvent(index, kind, FaultTarget.TRANSPORT, None, None)
                    )
            return tuple(plan)
        raise SoakError(f"soak_unknown_schedule_path:{path}")

    # -- identity -------------------------------------------------------- #

    def events_document(self) -> list[dict[str, Any]]:
        return [event.as_document() for event in self._events]

    def digest(self) -> str:
        payload = json.dumps(self.events_document(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TransportFaultStream:
    """Consumes the transport slice, one decision per frame the transport sends."""

    def __init__(self, schedule: FaultSchedule | None = None) -> None:
        self._plan: tuple[FaultEvent, ...] = (
            schedule.events_for("transport") if schedule is not None else ()
        )
        self._cursor = 0

    def decide(self) -> FaultEvent:
        if self._cursor >= len(self._plan):
            # The schedule is a floor, not a behaviour change: once it is
            # exhausted the remaining frames are clean.
            return FaultEvent(-1, FaultKind.NONE, FaultTarget.TRANSPORT, None, None)
        event = self._plan[self._cursor]
        self._cursor += 1
        return event

    def consumed(self) -> int:
        return self._cursor


# --------------------------------------------------------------------------- #
# Ledger and invariant checks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Violation:
    """One violated invariant, with the registry bound it was judged against."""

    code: str
    subject: str
    detail: str
    bound_name: str
    bound_value: float | int

    def as_document(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "subject": self.subject,
            "detail": self.detail,
            "bound_name": self.bound_name,
            "bound_value": self.bound_value,
        }


@dataclass
class ExecutionRecord:
    command_id: str
    host_id: str
    request_id: str | None
    placement_id: str | None
    phase: str | None
    token_index: int | None
    injected: bool
    at: float


@dataclass
class RowRecord:
    """A durable row (a gateway session), its progress and its retirement."""

    request_id: str
    registered_at: float
    last_progress_at: float
    bound_name: str
    bound_seconds: float
    scope: str = "mailbox"
    bound_origin: str = "last_progress"
    last_sequence: int = -1
    terminal_kind: str | None = None
    terminal_code: str | None = None
    terminal_at: float | None = None
    retired_at: float | None = None
    retired_with_terminal: bool | None = None
    retirement_kind: str = "product"
    retirement_note: str = ""
    stalled_at: float | None = None
    stall_note: str = ""
    closed_by_soak_at: float | None = None
    closed_by_soak_note: str = ""

    def observe(self, *, at: float, sequence: int) -> None:
        if sequence > self.last_sequence:
            self.last_sequence = sequence
            self.last_progress_at = at

    def observe_terminal(self, *, at: float, kind: str, code: str | None) -> None:
        if self.terminal_kind is None:
            self.terminal_kind = kind
            self.terminal_code = code
            self.terminal_at = at

    def retire(self, *, at: float, note: str, kind: str) -> None:
        if self.retired_at is not None:
            return
        self.retired_at = at
        self.retired_with_terminal = self.terminal_kind is not None
        self.retirement_kind = kind
        self.retirement_note = note

    def mark_stall(self, *, at: float, note: str) -> None:
        if self.stalled_at is None:
            self.stalled_at = at
            self.stall_note = note

    def close_by_soak(self, *, at: float, note: str) -> None:
        if self.closed_by_soak_at is None:
            self.closed_by_soak_at = at
            self.closed_by_soak_note = note

    def bound_origin_seconds(self, elapsed: float) -> float:
        """When the terminal budget started running for this row."""

        del elapsed
        if self.bound_origin == "registration":
            return self.registered_at
        return max(self.registered_at, self.last_progress_at)


@dataclass
class LoopObservation:
    subject: str
    at: float
    progress: bool
    membership_valid: bool
    terminal_seen: bool
    note: str = ""


@dataclass
class FaultApplication:
    step: int
    kind: str
    target: str
    label: str
    bound_name: str | None
    bound_seconds: float | None


@dataclass
class Ledger:
    """Everything the four invariants are judged on."""

    executions: list[ExecutionRecord] = field(default_factory=list)
    rows: dict[str, RowRecord] = field(default_factory=dict)
    loops: list[LoopObservation] = field(default_factory=list)
    applications: list[FaultApplication] = field(default_factory=list)
    requests_admitted: list[str] = field(default_factory=list)
    restarts: list[str] = field(default_factory=list)
    dropped_observations: list[str] = field(default_factory=list)
    named_rejections: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)

    # -- recording ------------------------------------------------------- #

    def now(self) -> float:
        return time.monotonic()

    def elapsed(self) -> float:
        return self.now() - self.started_at

    def admit_request(
        self,
        request_id: str,
        *,
        bound_name: str,
        bound_seconds: float,
        scope: str = "mailbox",
        bound_origin: str = "last_progress",
    ) -> None:
        self.requests_admitted.append(request_id)
        row = self.rows.get(request_id)
        if row is None:
            row = RowRecord(
                request_id=request_id,
                registered_at=self.elapsed(),
                last_progress_at=self.elapsed(),
                bound_name=bound_name,
                bound_seconds=bound_seconds,
                scope=scope,
                bound_origin=bound_origin,
            )
            self.rows[request_id] = row
        if scope == "mailbox":
            # Admission *is* the first liveness signal of the request's control
            # loop: without this the control-loop invariant would have no
            # "last progress" reference and could never judge a loop that stalls
            # from the start (which is exactly the defect it exists to catch).
            self.observe_loop(
                f"gateway-worker:{request_id}",
                progress=True,
                membership_valid=True,
                terminal_seen=False,
                note="admitted",
            )

    def record_execution(
        self,
        *,
        command_id: str,
        host_id: str,
        request_id: str | None,
        placement_id: str | None,
        phase: str | None,
        token_index: int | None,
        injected: bool = False,
    ) -> None:
        self.executions.append(
            ExecutionRecord(
                command_id=command_id,
                host_id=host_id,
                request_id=request_id,
                placement_id=placement_id,
                phase=phase,
                token_index=token_index,
                injected=injected,
                at=self.elapsed(),
            )
        )

    def observe_row_progress(self, request_id: str, sequence: int) -> None:
        row = self.rows.get(request_id)
        if row is not None:
            row.observe(at=self.elapsed(), sequence=sequence)

    def observe_row_terminal(self, request_id: str, *, kind: str, code: str | None) -> None:
        row = self.rows.get(request_id)
        if row is not None:
            row.observe_terminal(at=self.elapsed(), kind=kind, code=code)

    def retire_row(self, request_id: str, *, note: str, kind: str = "product") -> None:
        row = self.rows.get(request_id)
        if row is not None:
            row.retire(at=self.elapsed(), note=note, kind=kind)

    def close_row_by_soak(self, request_id: str, *, note: str) -> None:
        """The soak itself tore the row's service down (never a product signal)."""

        row = self.rows.get(request_id)
        if row is not None:
            row.close_by_soak(at=self.elapsed(), note=note)

    def mark_row_stall(self, request_id: str, *, note: str) -> None:
        row = self.rows.get(request_id)
        if row is not None:
            row.mark_stall(at=self.elapsed(), note=note)

    def observe_loop(
        self,
        subject: str,
        *,
        progress: bool,
        membership_valid: bool,
        terminal_seen: bool,
        note: str = "",
    ) -> None:
        self.loops.append(
            LoopObservation(
                subject=subject,
                at=self.elapsed(),
                progress=progress,
                membership_valid=membership_valid,
                terminal_seen=terminal_seen,
                note=note,
            )
        )

    def record_fault(self, event: FaultEvent, label: str) -> None:
        self.applications.append(
            FaultApplication(
                step=event.index,
                kind=str(event.kind),
                target=str(event.target),
                label=label,
                bound_name=event.bound_name,
                bound_seconds=event.bound_seconds,
            )
        )

    def record_rejection(self, code: str) -> None:
        self.named_rejections.append(code)

    def record_restart(self, label: str) -> None:
        self.restarts.append(label)

    def record_dropped_observation(self, label: str) -> None:
        self.dropped_observations.append(label)

    # -- checks ---------------------------------------------------------- #

    def check_invariants(self, bounds: SoakBounds | None = None) -> tuple[Violation, ...]:
        bounds = bounds or soak_bounds()
        return tuple(
            [
                *self._check_single_execution(),
                *self._check_nonterminal_retirement(bounds),
                *self._check_control_loop(bounds),
                *self._check_terminal_within_budget(),
                *self._check_stalled_rows(),
            ]
        )

    def _check_single_execution(self) -> list[Violation]:
        counts: dict[str, list[ExecutionRecord]] = {}
        for record in self.executions:
            counts.setdefault(record.command_id, []).append(record)
        violations: list[Violation] = []
        for command_id, records in sorted(counts.items()):
            if len(records) > MAXIMUM_NATIVE_EXECUTIONS_PER_COMMAND:
                violations.append(
                    Violation(
                        code=INVARIANT_AT_MOST_ONE_NATIVE_EXECUTION_PER_COMMAND,
                        subject=command_id,
                        detail=(
                            f"native_executions={len(records)} "
                            f"hosts={sorted({record.host_id for record in records})} "
                            f"placements={sorted({str(r.placement_id) for r in records})}"
                        ),
                        bound_name="maximum_native_executions_per_command",
                        bound_value=MAXIMUM_NATIVE_EXECUTIONS_PER_COMMAND,
                    )
                )
        return violations

    def _check_nonterminal_retirement(self, bounds: SoakBounds) -> list[Violation]:
        violations: list[Violation] = []
        for request_id, row in sorted(self.rows.items()):
            if row.retired_at is None or row.retired_with_terminal:
                continue
            if row.retirement_kind != "product":
                # Retirement performed by the soak's own teardown is not a
                # product signal; it is recorded, never judged.
                continue
            age = row.retired_at - row.registered_at
            if age < bounds.nonterminal_row_retirement_seconds:
                violations.append(
                    Violation(
                        code=INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND,
                        subject=request_id,
                        detail=(
                            f"retired_without_terminal_after_s={age:.6f} "
                            f"last_sequence={row.last_sequence} "
                            f"note={row.retirement_note!r}"
                        ),
                        bound_name="cancel_and_cleanup_budget_seconds",
                        bound_value=bounds.nonterminal_row_retirement_seconds,
                    )
                )
        return violations

    def _check_control_loop(self, bounds: SoakBounds) -> list[Violation]:
        violations: list[Violation] = []
        last_progress: dict[str, float] = {}
        reported: set[tuple[str, int]] = set()
        for observation in self.loops:
            subject = observation.subject
            if observation.progress:
                last_progress[subject] = observation.at
                continue
            reference = last_progress.get(subject)
            if reference is None:
                # Never observed alive: an unobserved subject, not a dead loop.
                continue
            if observation.terminal_seen or not observation.membership_valid:
                continue
            silent_for = observation.at - reference
            if silent_for < bounds.control_loop_stall_seconds:
                continue
            bucket = int(silent_for // bounds.control_loop_stall_seconds)
            key = (subject, bucket)
            if key in reported:
                continue
            reported.add(key)
            violations.append(
                Violation(
                    code=INVARIANT_CONTROL_LOOP_DEATH_WHILE_MEMBERSHIP_VALID,
                    subject=subject,
                    detail=(
                        f"no_progress_for_s={silent_for:.6f} membership=valid "
                        f"note={observation.note!r}"
                    ),
                    bound_name="liveness_active_failure_detection_ms",
                    bound_value=bounds.control_loop_stall_seconds,
                )
            )
        return violations

    def _check_terminal_within_budget(self) -> list[Violation]:
        """A row is judged against its own budget, measured from its own origin."""

        violations: list[Violation] = []
        elapsed = self.elapsed()
        for request_id, row in sorted(self.rows.items()):
            if row.terminal_kind is not None or row.closed_by_soak_at is not None:
                continue
            pending = elapsed - row.bound_origin_seconds(elapsed)
            if pending < row.bound_seconds:
                continue
            violations.append(
                Violation(
                    code=INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET,
                    subject=request_id,
                    detail=(
                        f"no_terminal_frame_within_s={pending:.6f} "
                        f"origin={row.bound_origin} last_sequence={row.last_sequence} "
                        f"retired={'yes' if row.retired_at is not None else 'no'}"
                    ),
                    bound_name=row.bound_name,
                    bound_value=row.bound_seconds,
                )
            )
        return violations

    def _check_stalled_rows(self) -> list[Violation]:
        """A stalled or cancelled row gets the owner's cancellation budget."""

        violations: list[Violation] = []
        bounds = soak_bounds()
        elapsed = self.elapsed()
        for request_id, row in sorted(self.rows.items()):
            if row.stalled_at is None or row.terminal_kind is not None:
                continue
            if row.closed_by_soak_at is not None:
                continue
            pending = elapsed - row.stalled_at
            if pending < bounds.request_terminal_cancelled_seconds:
                continue
            violations.append(
                Violation(
                    code=INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET,
                    subject=request_id,
                    detail=(
                        f"no_terminal_frame_after_stall_s={pending:.6f} "
                        f"stall_note={row.stall_note!r} "
                        f"last_sequence={row.last_sequence}"
                    ),
                    bound_name="cancel_and_cleanup_budget_seconds",
                    bound_value=bounds.request_terminal_cancelled_seconds,
                )
            )
        return violations

    # -- digest ---------------------------------------------------------- #

    def application_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for application in self.applications:
            key = f"{application.kind}:{application.target}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def execution_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.executions:
            counts[record.command_id] = counts.get(record.command_id, 0) + 1
        return counts

    def terminal_classes(self) -> dict[str, str]:
        return {
            request_id: (row.terminal_kind or "none")
            for request_id, row in sorted(self.rows.items())
        }

    def soak_material(self) -> dict[str, Any]:
        """Soak-determined material: what identifies a run, nothing measured.

        Transport-target applications are excluded because the number of frames a
        request sends is data-dependent (a request that fails early sends fewer),
        and so is the ack watermark a restart resumes from.  Mailbox and runtime
        applications are one per schedule step, so they are deterministic.
        """

        counts = {
            key: value
            for key, value in sorted(self.application_counts().items())
            if not key.endswith(f":{FaultTarget.TRANSPORT}")
        }
        return {
            "schema": SOAK_SCHEMA_VERSION,
            "application_counts": counts,
        }

    def product_material(self) -> dict[str, Any]:
        """Observed product material: recorded, never part of the replay digest."""

        return {
            "execution_counts": self.execution_counts(),
            "terminal_classes": self.terminal_classes(),
            "requests_admitted": sorted(self.requests_admitted),
            "named_rejections": sorted(self.named_rejections),
            "dropped_observations": sorted(self.dropped_observations),
            "restarts": sorted(self.restarts),
            "transport_application_counts": {
                key: value
                for key, value in sorted(self.application_counts().items())
                if key.endswith(f":{FaultTarget.TRANSPORT}")
            },
        }


# --------------------------------------------------------------------------- #
# Seeded injection seams (the seams mycelium_testbed.iroh_e2e already uses)
# --------------------------------------------------------------------------- #


class SoakSession:
    """The active soak state the injection seams read."""

    def __init__(
        self,
        *,
        seed: int,
        bounds: SoakBounds,
        time_scale: float = 1.0,
        schedule: FaultSchedule | None = None,
    ) -> None:
        self.seed = seed
        self.bounds = bounds
        self.time_scale = float(time_scale)
        self.ledger = Ledger()
        self.transport_faults = TransportFaultStream(schedule)
        self.double_execution: bool = False
        self.stall_seconds: float | None = None
        self.stall_latch = threading.Event()
        self.stall_complete = threading.Event()
        self.retire_nonterminal: bool = False
        self.drop_terminal: bool = False
        self.peer_kill_requested: bool = False
        self.sidecars: dict[str, Any] = {}
        self.peer_map: dict[str, str] = {}
        self.placement_hosts: dict[str, str] = {}
        self.request_id: str | None = None

    def stall_scale(self) -> float:
        return self.time_scale

    def register_sidecar(
        self, node_id: str, sidecar: Any, peer_node_id: str | None = None
    ) -> None:
        self.sidecars[node_id] = sidecar
        if peer_node_id:
            self.peer_map[node_id] = peer_node_id

    def host_for_placement(self, placement_id: str | None) -> str:
        if isinstance(placement_id, str):
            host = self.placement_hosts.get(placement_id)
            if host:
                return host
        return "unknown"

    def sidecar_running(self, node_id: str) -> bool:
        sidecar = self.sidecars.get(node_id)
        if sidecar is None:
            return False
        process = getattr(sidecar, "process", None)
        if process is None:
            return False
        return process.poll() is None

    def kill_peer(self, node_id: str) -> bool:
        """Stop the peer process of ``node_id`` - a real control-loop death."""

        peer_node_id = self.peer_map.get(node_id)
        if peer_node_id is None:
            return False
        sidecar = self.sidecars.get(peer_node_id)
        if sidecar is None or not self.sidecar_running(peer_node_id):
            return False
        self.ledger.record_restart(f"peer_process_stopped:{peer_node_id}")
        sidecar.stop()
        return True


_ACTIVE_SOAK: SoakSession | None = None
_ACTIVE_SOAK_LOCK = threading.Lock()


def active_soak() -> SoakSession | None:
    with _ACTIVE_SOAK_LOCK:
        return _ACTIVE_SOAK


@contextmanager
def activate_soak(session: SoakSession) -> Iterator[SoakSession]:
    global _ACTIVE_SOAK
    with _ACTIVE_SOAK_LOCK:
        previous = _ACTIVE_SOAK
        _ACTIVE_SOAK = session
    try:
        yield session
    finally:
        with _ACTIVE_SOAK_LOCK:
            _ACTIVE_SOAK = previous


class SoakTransport(iroh_e2e.TracedIrohTransport):
    """The traced transport plus the seeded ``duplicate`` / ``delay`` faults.

    Recording is unchanged (the parent class already emits ``enqueue`` /
    ``receipt`` / ``delivered``); this subclass only decides whether one more
    frame goes out, or a pause is taken first.
    """

    def send_router_frame(self, frame: bytes, **kwargs: Any):  # type: ignore[override]
        session = active_soak()
        if session is None:
            return super().send_router_frame(frame, **kwargs)
        event = session.transport_faults.decide()
        if event.kind is FaultKind.DELAY and event.bound_seconds:
            session.ledger.record_fault(event, "delay_applied")
            time.sleep(event.bound_seconds * session.stall_scale())
        receipt = super().send_router_frame(frame, **kwargs)
        if event.kind is FaultKind.DUPLICATE:
            # A real duplicate on the wire: the same command payload is put on
            # the control path a second time.  The transport mints a fresh
            # message id per send, which is exactly how a duplicated mailbox
            # delivery looks to the receiver.
            duplicate = super().send_router_frame(frame, **kwargs)
            session.ledger.record_fault(event, "duplicate_sent")
            recorder = getattr(self, "_testbed_recorder", None)
            if recorder is not None:
                recorder.event(
                    EventKind.ENQUEUE,
                    detail={
                        "origin": "soak.transport.duplicate",
                        "message_id": duplicate.message_id.hex(),
                        "soak_step": event.index,
                    },
                )
        return receipt


class SoakStageRuntime(iroh_e2e.TracedStageRuntime):
    """The traced stage runtime plus the execution ledger and the falsification faults."""

    def _record(self, item: Any, *, injected: bool) -> None:
        session = active_soak()
        if session is None:
            return
        session.ledger.record_execution(
            command_id=iroh_e2e.command_id_for(item),
            host_id=session.host_for_placement(getattr(item, "placement_id", None)),
            request_id=getattr(item, "request_id", None),
            placement_id=getattr(item, "placement_id", None),
            phase=getattr(item, "phase", None),
            token_index=getattr(item, "token_index", None),
            injected=injected,
        )

    def _maybe_stall(self) -> None:
        """One-shot control-loop stall (falsification), not a schedule fault."""

        session = active_soak()
        if session is None or session.stall_seconds is None:
            return
        seconds = session.stall_seconds * session.stall_scale()
        session.stall_seconds = None
        session.stall_latch.set()
        try:
            time.sleep(seconds)
        finally:
            session.stall_complete.set()

    def _maybe_double(self, item: Any) -> None:
        session = active_soak()
        if session is None or not session.double_execution:
            return
        self._record(item, injected=True)
        self._testbed_inner.execute(item)

    def execute(self, item: Any) -> Any:
        self._maybe_stall()
        self._record(item, injected=False)
        result = super().execute(item)
        self._maybe_double(item)
        return result

    def execute_batch(self, batch: Any) -> Any:
        items = tuple(getattr(batch, "items", ()))
        for item in items:
            self._maybe_stall()
            self._record(item, injected=False)
        results = super().execute_batch(batch)
        if active_soak() is not None:
            for item in items:
                self._maybe_double(item)
        return results


def load_harness() -> Any:
    """The existing e2e harness, imported through iroh_e2e's own loader."""

    return iroh_e2e._load_iroh_harness()


@contextmanager
def soak_seams(
    session: SoakSession,
    *,
    patch_runtime: bool = False,
    recorder_for_placement: Callable[[str], Any] | None = None,
) -> Iterator[None]:
    """Patch the *test-harness* factories with the soak's subclasses.

    No product behaviour changes: only the harness's own factories are replaced
    (``harness._make_transport`` always, ``harness._DeterministicStageRuntime``
    when ``patch_runtime`` is set), and ``mycelium_testbed.iroh_e2e``'s traced
    classes - which the reuse adapter itself substitutes for exactly one run -
    are swapped for soak subclasses.

    ``patch_runtime`` is off for the canonical-request episode on purpose:
    ``iroh_e2e.run_traced_complete_request`` already patches
    ``harness._DeterministicStageRuntime`` with its own factory, and patching it
    twice would wrap the runtime twice (every execution would be recorded twice,
    which would look like a double execution).  The mailbox episode builds its
    own topology, so it asks for the runtime patch explicitly.
    """

    harness = load_harness()
    real_make_transport = harness._make_transport
    real_runtime_class = harness._DeterministicStageRuntime
    try:
        session.placement_hosts.update(iroh_e2e.placement_hosts(harness))
    except TraceError:  # pragma: no cover - graph guard
        pass

    def transport_factory(
        node_id: str, sidecar: Any, peer_node_id: str, peer: Any, **kwargs: Any
    ) -> Any:
        session.register_sidecar(node_id, sidecar, peer_node_id)
        session.register_sidecar(peer_node_id, peer, node_id)
        return real_make_transport(node_id, sidecar, peer_node_id, peer, **kwargs)

    def runtime_factory(**kwargs: Any) -> Any:
        if recorder_for_placement is None:
            raise SoakError("soak_runtime_patch_requires_recorder_for_placement")
        return SoakStageRuntime(
            inner=real_runtime_class(**kwargs),
            recorder_for_placement=recorder_for_placement,
        )

    with iroh_e2e._patched(harness, "_make_transport", transport_factory):
        with iroh_e2e._patched(iroh_e2e, "TracedIrohTransport", SoakTransport):
            with iroh_e2e._patched(iroh_e2e, "TracedStageRuntime", SoakStageRuntime):
                if patch_runtime:
                    with iroh_e2e._patched(
                        harness, "_DeterministicStageRuntime", runtime_factory
                    ):
                        yield
                else:
                    yield


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SoakConfig:
    """One sweep's configuration.

    Every number here is either a registry bound (through :class:`SoakBounds`)
    or a soak control (seed list, event count, session-table size, time scale),
    never a product deadline.
    """

    seeds: tuple[int, ...]
    events: int = 200
    run_id: str = DEFAULT_RUN_ROOT_NAME
    out_root: Path | None = None
    native_binary: Path | None = None
    weights: FaultWeights = field(default_factory=FaultWeights)
    forced: tuple[ForcedFault, ...] = ()
    real_request: bool = True
    mailbox: bool = True
    max_sessions: int = 2
    time_scale: float = 1.0
    fsync: bool = False
    bounds: SoakBounds | None = None

    def resolved_bounds(self) -> SoakBounds:
        return self.bounds or soak_bounds()

    def run_root(self) -> Path:
        root = self.out_root or (DEFAULT_RUNS_DIR / DEFAULT_RUN_ROOT_NAME)
        return Path(root) / self.run_id

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": SOAK_SCHEMA_VERSION,
            "seeds": list(self.seeds),
            "events": self.events,
            "run_id": self.run_id,
            "out_root": str(self.out_root) if self.out_root else None,
            "native_binary": str(self.native_binary) if self.native_binary else None,
            "weights": self.weights.as_document(),
            "forced": [fault.as_document() for fault in self.forced],
            "real_request": self.real_request,
            "mailbox": self.mailbox,
            "max_sessions": self.max_sessions,
            "time_scale": self.time_scale,
            "bounds": self.resolved_bounds().limits(),
            "bounds_digest": self.resolved_bounds().digest(),
        }


@dataclass(frozen=True)
class SeedResult:
    seed: int
    label: str
    schedule_digest: str
    run_digest: str
    product_digest: str
    violations: tuple[Violation, ...]
    application_counts: Mapping[str, int]
    execution_counts: Mapping[str, int]
    terminal_classes: Mapping[str, str]
    events: int
    trace_dirs: tuple[str, ...]
    replayed: bool
    wall_seconds: float
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def invariant_codes_fired(self) -> tuple[str, ...]:
        return tuple(sorted({violation.code for violation in self.violations}))

    @property
    def green(self) -> bool:
        return not self.violations

    def replay_command(self) -> str:
        return (
            "cd /Users/evinova-self/Documents/playground/mycelium-core-pilot && "
            "env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 "
            f"/opt/homebrew/bin/python3 -B -m mycelium_testbed.soak "
            f"--seed {self.seed} --events {self.events} --run-id <run-id>"
        )

    def as_document(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "label": self.label,
            "schedule_digest": self.schedule_digest,
            "run_digest": self.run_digest,
            "product_digest": self.product_digest,
            "green": self.green,
            "violations": [violation.as_document() for violation in self.violations],
            "invariant_codes_fired": list(self.invariant_codes_fired),
            "application_counts": dict(self.application_counts),
            "execution_counts": dict(self.execution_counts),
            "terminal_classes": dict(self.terminal_classes),
            "events": self.events,
            "trace_dirs": list(self.trace_dirs),
            "replayed": self.replayed,
            "wall_seconds": round(self.wall_seconds, 6),
            "replay_command": self.replay_command(),
            "detail": dict(self.detail),
        }


# --------------------------------------------------------------------------- #
# Path A: the canonical real request, with seeded transport faults
# --------------------------------------------------------------------------- #


def _terminal_from_trace(
    events: Sequence[Any], request_id: str
) -> tuple[str, str | None] | None:
    """The traced terminal of a request: success, or a named failure.

    A trace can carry more than one ``terminal`` event for a request (the
    harness's teardown can race a deferred forward, see
    ``mycelium_testbed/DESIGN_NOTES.md`` §5).  A ``completed`` terminal therefore
    wins over any other; otherwise the first non-``completed`` terminal is the
    request's named failure.
    """

    terminals = [
        event
        for event in events
        if event.kind is EventKind.TERMINAL
        and event.request_id in (None, request_id)
    ]
    for event in terminals:
        if str(event.status) == "completed":
            return "completed", None
    if not terminals:
        return None
    event = terminals[0]
    status = str(event.status)
    code = None
    if isinstance(event.detail, Mapping):
        candidate = event.detail.get("code") or event.detail.get("reason")
        if isinstance(candidate, str):
            code = candidate
    return "failed", (code or status)


def _terminal_from_run_dir(run_dir: Path, request_id: str) -> tuple[str, str | None] | None:
    """Re-read the trace after a driver raise: the terminal is still on disk."""

    header = Path(run_dir) / "run.json"
    if not header.is_file():
        return None
    try:
        loaded = load_run(run_dir)
    except TraceError:  # pragma: no cover - unreadable trace guard
        return None
    return _terminal_from_trace(loaded.events, request_id)


def _run_real_request(
    *, config: SoakConfig, session: SoakSession, run_dir: Path
) -> dict[str, Any]:
    """Drive ``harness.run_complete_request`` through ``iroh_e2e``'s trace adapter."""

    binary = config.native_binary
    if binary is None:
        return {"skipped": "no_native_binary"}
    harness = load_harness()
    request_id = str(harness.REQUEST_ID)
    session.request_id = request_id
    session.ledger.admit_request(
        request_id,
        bound_name="node_command_timeout_seconds",
        bound_seconds=config.resolved_bounds().request_terminal_driver_seconds,
        scope="real_request",
        bound_origin="registration",
    )
    started = time.monotonic()
    traced = None
    raised: str | None = None
    try:
        with soak_seams(session):
            traced = iroh_e2e.run_traced_complete_request(
                binary, run_dir, fsync=config.fsync
            )
    except BaseException as error:  # the driver's own assertions trip on faults
        raised = f"{type(error).__name__}:{str(error)[:300]}"
        session.ledger.record_dropped_observation(f"real_request_raised:{raised}")
    elapsed = time.monotonic() - started
    terminal: tuple[str, str | None] | None = None
    if traced is not None:
        terminal = _terminal_from_trace(traced.events, request_id)
    if terminal is None:
        # A raise still leaves the traced terminal frame on disk; the terminal
        # frame, not the driver's own assertion, is what the invariant judges.
        terminal = _terminal_from_run_dir(run_dir, request_id)
    if terminal is not None:
        session.ledger.observe_row_terminal(request_id, kind=terminal[0], code=terminal[1])
    elif elapsed >= config.resolved_bounds().control_loop_stall_seconds:
        session.ledger.mark_row_stall(
            request_id, note=f"real_request_no_terminal:{raised}"
        )
    for node_id in sorted(session.sidecars):
        session.ledger.observe_loop(
            f"peer-process:{node_id}",
            progress=session.sidecar_running(node_id),
            membership_valid=True,
            terminal_seen=terminal is not None,
            note="post_real_request",
        )
    evidence: dict[str, Any] = {
        "request_id": request_id,
        "wall_seconds": round(elapsed, 6),
        "terminal": list(terminal) if terminal else None,
    }
    if raised is not None:
        evidence["raised"] = raised
    if traced is not None:
        product = traced.evidence
        evidence.update(
            {
                "accepted_status": product.accepted_status,
                "router_admissions": product.router_admissions,
                "token_indexes": list(product.token_indexes),
                "decode_route_steps": product.decode_route_steps,
                "acknowledged_cursor": product.acknowledged_cursor,
                "transport_type": product.transport_type,
                "trace_events": len(traced.events),
            }
        )
    return evidence


# --------------------------------------------------------------------------- #
# Path B: the mailbox soak (enqueue / poll / ack / result)
# --------------------------------------------------------------------------- #


class MailboxEpisode:
    """The real gateway mailbox, driven by the seeded schedule."""

    def __init__(self, *, config: SoakConfig, session: SoakSession, run_dir: Path) -> None:
        self.config = config
        self.session = session
        self.run_dir = run_dir
        self.bounds = config.resolved_bounds()
        self.harness: Any = None
        self.topology: Any = None
        self.service: Any = None
        self.qualification: Any = None
        self.source: Any = None
        self.port: Any = None
        self.backend: Any = None
        self.app: Any = None
        self.submission: Any = None
        self.subscription: Any = None
        self.owner_token: str | None = None
        self.request_id: str | None = None
        self.recorder: TraceRecorder | None = None
        self.run: TraceRunDirectory | None = None
        self._request_counter = 0
        self._last_ack: int | None = None
        self.events_observed = 0

    # -- lifecycle ------------------------------------------------------- #

    def start(self) -> None:
        harness = load_harness()
        self.harness = harness
        binary = self.config.native_binary
        if binary is None:
            raise SoakError("soak_mailbox_requires_native_binary")
        self.run = TraceRunDirectory(
            self.run_dir,
            run_id=self.run_dir.name,
            host_id="node-a",
            reference_host="node-a",
            fsync=self.config.fsync,
            notes=(
                "seeded mailbox soak (enqueue/poll/ack/result) over the real "
                "RequestGatewayService; faults from mycelium_testbed.soak"
            ),
        )
        self.recorder = TraceRecorder("node-a", run=self.run, role="soak_mailbox")
        self._recorders = {"node-a": self.recorder}
        self._build_stack()

    def _recorder_for_placement(self, placement_id: str) -> TraceRecorder:
        host_id = self.session.placement_hosts.get(placement_id)
        if host_id is None:
            raise SoakError(f"soak_unknown_placement_host:{placement_id}")
        recorder = self._recorders.get(host_id)
        if recorder is None:
            if self.run is None:
                raise SoakError("soak_recorder_without_run_directory")
            recorder = TraceRecorder(host_id, run=self.run, role="soak_mailbox")
            self._recorders[host_id] = recorder
        return recorder

    def _build_stack(self) -> None:
        harness = self.harness
        assert harness is not None
        with soak_seams(
            self.session,
            patch_runtime=True,
            recorder_for_placement=self._recorder_for_placement,
        ):
            self.topology = harness._Topology(self.config.native_binary)
            stack = harness._service_stack(self.topology)
        (
            self.qualification,
            self.source,
            self.port,
            self.backend,
            self.service,
            self.app,
            self.submission,
        ) = stack
        # The harness fixes a single request id (``_FixedRequestId``); the soak
        # installs its own seeded source so one episode can admit more than one
        # request (needed for the peer-restart rebuild and for spreading the
        # event budget across several request lifecycles).  Everything else in
        # the stack is the harness's own service, backend, router port and app.
        self.service._request_id_source = self._next_request_id
        self.source.value = self.qualification

    def close(self) -> None:
        errors: list[BaseException] = []
        if self.service is not None:
            try:
                self.service.close()
            except BaseException as error:  # pragma: no cover - teardown guard
                errors.append(error)
        if self.run is not None:
            try:
                self.run.close()
            except BaseException as error:  # pragma: no cover - teardown guard
                errors.append(error)
        if self.topology is not None:
            try:
                self.topology.close()
            except BaseException as error:  # pragma: no cover - teardown guard
                errors.append(error)
        if errors:
            raise errors[0]

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return f"request-soak-{self.session.seed:04d}-{self._request_counter:03d}"

    # -- enqueue --------------------------------------------------------- #

    def admit(self) -> str:
        harness = self.harness
        status, body = harness._post(self.app, self.submission)
        if status != 202:
            raise SoakError(f"soak_admission_failed:{status}:{body}")
        request_id = body.get("request_id")
        token = body.get("session_token")
        if not isinstance(request_id, str) or not isinstance(token, str):
            raise SoakError("soak_admission_without_request_id_or_token")
        self.request_id = request_id
        self.owner_token = token
        self.session.ledger.admit_request(
            request_id,
            bound_name="cancel_and_cleanup_budget_seconds",
            bound_seconds=self.bounds.request_terminal_cancelled_seconds,
            scope="mailbox",
            bound_origin="last_progress",
        )
        if self.recorder is not None:
            self.recorder.event(
                EventKind.ENQUEUE,
                request_id=request_id,
                detail={"origin": "soak.gateway_submit", "status": status},
            )
        self._subscribe(last_event_id=None, note="enqueue")
        return request_id

    def _subscribe(self, *, last_event_id: int | None, note: str) -> None:
        request_id = self.request_id
        if request_id is None:
            raise SoakError("soak_subscribe_without_request")
        if self.subscription is not None and not self.subscription.closed:
            self.subscription.close()
        try:
            self.subscription = self.service.subscribe(
                request_id,
                last_event_id=last_event_id,
                owner_token=self.owner_token,
            )
        except Exception as error:  # noqa: BLE001 - the product's named rejection
            code = _named_code(error)
            self.session.ledger.record_rejection(code)
            if code == "unknown_request":
                # The row is gone while the request has no terminal frame: the
                # ledger classifies that as a non-terminal retirement.
                self.session.ledger.retire_row(request_id, note=f"unknown_request@{note}")
            if self.recorder is not None:
                self.recorder.event(
                    EventKind.TERMINAL,
                    request_id=request_id,
                    status=f"rejected:{code}",
                    detail={"origin": "soak.gateway_subscribe", "note": note},
                )
            raise SoakError(f"soak_resubscribe_rejected:{code}") from error
        self._last_ack = last_event_id

    # -- the seeded event loop ------------------------------------------- #

    def run_steps(self, schedule: FaultSchedule) -> None:
        """Consume the whole schedule: one mailbox event per step, request lifecycles
        back to back, so ``--events`` is the number of mailbox events actually run."""

        while schedule.consumed < schedule.events:
            event = schedule.next_fault()
            label = self._apply(event)
            self.session.ledger.record_fault(event, label)
            self._observe()
            if event.kind is FaultKind.RESTART_PEER:
                self._restart_peer()
            if self._row_terminal():
                self._close_request()
                if schedule.consumed >= schedule.events:
                    break
                self.admit()

    def _close_request(self) -> None:
        """The request reached its terminal: release the consumer and move on."""

        if self.subscription is not None and not self.subscription.closed:
            self.subscription.close()
        self.subscription = None

    def _apply(self, event: FaultEvent) -> str:
        kind = event.kind
        if kind is FaultKind.NONE:
            return self._poll_and_ack()
        if kind is FaultKind.DROP:
            # A dropped mailbox event: the consumer skips this polling window.
            if self.recorder is not None:
                self.recorder.event(
                    EventKind.DELIVERED,
                    request_id=self.request_id,
                    status="dropped",
                    detail={"origin": "soak.mailbox.drop", "soak_step": event.index},
                )
            return "poll_window_dropped"
        if kind is FaultKind.DUPLICATE:
            return self._duplicate()
        if kind is FaultKind.DELAY:
            time.sleep((event.bound_seconds or 0.0) * self.config.time_scale)
            return f"delayed:{event.bound_name}"
        if kind is FaultKind.REORDER:
            return self._reorder()
        if kind is FaultKind.RESTART_OWNER:
            return self._restart_owner()
        if kind is FaultKind.RESTART_PEER:
            return "peer_restart_requested"
        if kind is FaultKind.DOUBLE_EXECUTION:
            self.session.double_execution = True
            return "double_execution_armed"
        if kind is FaultKind.STALL_CONTROL_LOOP:
            self.session.stall_seconds = event.bound_seconds
            return "control_loop_stall_armed"
        if kind is FaultKind.RETIRE_NONTERMINAL_ROW:
            self._retire_nonterminal_row(note="forced_retire_nonterminal_row")
            return "nonterminal_row_retired"
        if kind is FaultKind.DROP_TERMINAL_FRAME:
            self.session.drop_terminal = True
            return "terminal_frame_drop_armed"
        raise SoakError(f"soak_fault_not_implemented:{kind}")

    def _poll_and_ack(self) -> str:
        if self.subscription is None:
            return "poll_without_subscription"
        try:
            event = self.subscription.next_event(timeout=self.bounds.poll_seconds)
        except TimeoutError:
            self.session.ledger.record_rejection("stream_event_timeout")
            return "poll_timeout"
        if event is None:
            return "poll_terminal_read"
        self.events_observed += 1
        self.session.ledger.observe_row_progress(event.request_id, event.sequence)
        try:
            self.subscription.ack(event.sequence)
        except Exception as error:  # noqa: BLE001
            self.session.ledger.record_rejection(_named_code(error))
        self._last_ack = event.sequence
        if event.terminal:
            self._record_terminal(event)
        return f"polled:{event.kind}"

    def _duplicate(self) -> str:
        """Deliver the last event again: re-issue its ack and re-deliver the frame."""

        if self.subscription is None or self._last_ack is None:
            return "duplicate_noop_no_event"
        try:
            self.subscription.ack(self._last_ack)
            label = "duplicate_ack_accepted"
        except Exception as error:  # noqa: BLE001 - the product's named rejection
            code = _named_code(error)
            self.session.ledger.record_rejection(code)
            label = f"duplicate_ack_rejected:{code}"
        if self.recorder is not None:
            self.recorder.event(
                EventKind.DELIVERED,
                request_id=self.request_id,
                status="duplicate",
                detail={"origin": "soak.mailbox.duplicate", "sequence": self._last_ack},
            )
        return label

    def _reorder(self) -> str:
        """Resume from an older cursor than the ack watermark (out-of-order replay)."""

        if self._last_ack is None:
            return "reorder_noop_no_cursor"
        older = max(-1, self._last_ack - 1)
        try:
            self._subscribe(last_event_id=older, note="reorder")
        except SoakError as error:
            return f"reorder_rejected:{error}"
        if self.recorder is not None:
            self.recorder.event(
                EventKind.DELIVERED,
                request_id=self.request_id,
                status="replay_from_older_cursor",
                detail={"origin": "soak.mailbox.reorder", "cursor": older},
            )
        return f"replayed_from_cursor:{older}"

    def _restart_owner(self) -> str:
        """Owner restart: drop the subscription and resume from the ack watermark."""

        resumed_from = self._last_ack
        try:
            self._subscribe(last_event_id=resumed_from, note="restart_owner")
        except SoakError as error:
            return f"owner_restart_rejected:{error}"
        self.session.ledger.record_restart(f"owner_subscription:{resumed_from}")
        if self.recorder is not None:
            self.recorder.event(
                EventKind.ENQUEUE,
                request_id=self.request_id,
                status="owner_restart",
                detail={
                    "origin": "soak.mailbox.restart_owner",
                    "resumed_from": resumed_from,
                },
            )
        return f"owner_restarted_from:{resumed_from}"

    def _retire_nonterminal_row(self, *, note: str) -> None:
        """Falsification: retire a durable row that has no terminal frame yet.

        The retirement uses the gateway's own reclaim action (latch cancellation,
        then remove the row from the session table, exactly as
        ``_make_session_room_locked`` does for an abandoned subscriber); the soak
        only does it earlier than the bound allows, which is the defect being
        falsified.
        """

        request_id = self.request_id
        if request_id is None or self.service is None:
            return
        session = self.service._sessions.get(request_id)
        if session is None:
            return
        if getattr(session, "terminal_event", None) is not None:
            return
        self.service._cancel_session(session)
        del self.service._sessions[request_id]
        self.session.ledger.retire_row(request_id, note=note)
        if self.recorder is not None:
            self.recorder.event(
                EventKind.TERMINAL,
                request_id=request_id,
                status="row_retired_nonterminal",
                detail={"origin": "soak.falsification.retire_nonterminal_row"},
            )

    # -- peer restart ----------------------------------------------------- #

    def _restart_peer(self) -> None:
        """Stop the peer process, observe the window, then restart owner and peer.

        The stop is a real process death (``_RunningSidecar.stop``) and the restart
        is a real rebuild of the topology, so the window is a restart window, not a
        fabricated stall: if it outlives the registry's active-failure detection
        bound with the membership still valid, the control-loop invariant fires on
        its own.
        """

        peer_node_id = self.session.peer_map.get("node-a", "node-c")
        killed = self.session.kill_peer("node-a")
        self.session.ledger.observe_loop(
            f"peer-process:{peer_node_id}",
            progress=self.session.sidecar_running(peer_node_id),
            membership_valid=not self._row_terminal(),
            terminal_seen=self._row_terminal(),
            note="peer_process_stopped" if killed else "peer_process_stop_noop",
        )
        if killed:
            self._rebuild_after_peer_loss()

    def _rebuild_after_peer_loss(self) -> None:
        """Restart owner and peer processes on a fresh topology (a real restart)."""

        previous = self.request_id
        self.session.ledger.record_restart("owner_and_peer_rebuilt")
        if previous is not None and not self._row_terminal():
            row = self.session.ledger.rows.get(previous)
            if row is not None and row.stalled_at is None:
                # The soak itself is stopping the service here (the restart), so
                # the row's fate is a soak-side fact, never judged as a product
                # retirement or as a missing terminal.
                self.session.ledger.close_row_by_soak(
                    previous, note="service_rebuilt_by_soak_after_peer_restart"
                )
        self._close_request()
        self._last_ack = None
        if self.service is not None:
            self.service.close()
        if self.topology is not None:
            self.topology.close()
        self.service = None
        self._build_stack()
        self.admit()

    # -- observation ------------------------------------------------------ #

    def _row_terminal(self, request_id: str | None = None) -> bool:
        request_id = request_id or self.request_id
        if request_id is None:
            return False
        row = self.session.ledger.rows.get(request_id)
        return row is not None and row.terminal_kind is not None

    def _record_terminal(self, event: Any) -> None:
        if self.session.drop_terminal:
            # Falsification: the terminal frame exists in the product but never
            # reaches the client - "a request without a terminal frame".
            self.session.ledger.record_dropped_observation(
                f"terminal_frame_dropped:{event.request_id}"
            )
            self.session.ledger.mark_row_stall(
                event.request_id, note="terminal_frame_dropped"
            )
            if self.recorder is not None:
                self.recorder.event(
                    EventKind.DELIVERED,
                    request_id=event.request_id,
                    status="terminal_frame_dropped",
                    detail={"origin": "soak.mailbox.drop_terminal_frame"},
                )
            return
        self.session.ledger.observe_row_terminal(
            event.request_id, kind=event.kind, code=getattr(event, "code", None)
        )
        if self.recorder is not None:
            self.recorder.event(
                EventKind.TERMINAL,
                request_id=event.request_id,
                status=event.kind,
                detail={
                    "origin": "soak.mailbox.result",
                    "sequence": event.sequence,
                    "code": getattr(event, "code", None),
                    "token_index": getattr(event, "token_index", None),
                },
            )

    def _observe(self) -> None:
        """Read the real session table and feed the invariant ledger.

        Only the row the current service instance owns is read against that
        table: a row from a previous request lifecycle (or from the canonical
        request episode) is never "retired" because it is missing from a table
        that never held it.  The current row is the one whose table membership is
        evidence about the product.
        """

        service = self.service
        if service is None or self.request_id is None:
            return
        table = dict(service._sessions)
        for request_id in list(self.session.ledger.rows):
            row = self.session.ledger.rows[request_id]
            if row.scope != "mailbox":
                continue
            session = table.get(request_id)
            if session is None:
                if (
                    request_id == self.request_id
                    and row.retired_at is None
                    and row.terminal_kind is None
                ):
                    self.session.ledger.retire_row(
                        request_id, note="absent_from_session_table"
                    )
                continue
            if row.terminal_kind is not None or row.closed_by_soak_at is not None:
                continue
            with session.condition:
                sequence = int(session.latest_sequence)
                terminal = session.terminal_event
                worker_done = bool(session.worker_done)
                attached = bool(session.active_subscription)
            previous_sequence = row.last_sequence
            self.session.ledger.observe_row_progress(request_id, sequence)
            if terminal is not None and not self.session.drop_terminal:
                # The client's terminal frame is what the invariant counts.  When
                # the falsification latch is set the frame was dropped on the
                # client's side, so the product-side terminal in the session table
                # is deliberately *not* credited as the client's terminal.
                self.session.ledger.observe_row_terminal(
                    request_id,
                    kind=str(getattr(terminal, "kind", "unknown")),
                    code=getattr(terminal, "code", None),
                )
                continue
            row = self.session.ledger.rows[request_id]
            progress = sequence > previous_sequence or worker_done
            if (
                not progress
                and attached
                and (self.session.ledger.elapsed() - row.last_progress_at)
                >= self.bounds.control_loop_stall_seconds
            ):
                self.session.ledger.mark_row_stall(request_id, note="no_progress_observed")
            self.session.ledger.observe_loop(
                f"gateway-worker:{request_id}",
                progress=progress,
                membership_valid=attached,
                terminal_seen=False,
                note="mailbox_step",
            )
        for node_id in sorted(self.session.sidecars):
            self.session.ledger.observe_loop(
                f"peer-process:{node_id}",
                progress=self.session.sidecar_running(node_id),
                membership_valid=not self._row_terminal(),
                terminal_seen=self._row_terminal(),
                note="mailbox_step_peer",
            )

    def finish(self) -> None:
        """Wait, inside the registry budget, for the terminal frame to land."""

        if self.service is None or self.request_id is None:
            return
        deadline = time.monotonic() + self.bounds.request_terminal_cancelled_seconds
        while time.monotonic() < deadline and not self._row_terminal():
            if self.subscription is None:
                break
            self._poll_and_ack()
            self._observe()
        self._observe()


def _named_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    text = str(error)
    if text:
        return text.split(":", 1)[0][:64]
    return type(error).__name__


# --------------------------------------------------------------------------- #
# One seed, one sweep
# --------------------------------------------------------------------------- #


def _digest(payload: Mapping[str, Any]) -> str:
    document = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def _label_counts(labels: Sequence[str]) -> dict[str, int]:
    """Counts by label kind (everything before the first colon)."""

    counts: dict[str, int] = {}
    for label in labels:
        kind = label.split(":", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def run_seed(
    config: SoakConfig,
    seed: int,
    *,
    label: str | None = None,
    replayed: bool = False,
) -> SeedResult:
    """Run one seed end to end and return its digest-bearing result."""

    started = time.monotonic()
    bounds = config.resolved_bounds()
    schedule = FaultSchedule(
        seed=seed,
        events=config.events,
        weights=config.weights,
        forced=config.forced,
    )
    session = SoakSession(
        seed=seed,
        bounds=bounds,
        time_scale=config.time_scale,
        schedule=schedule,
    )
    label = label or f"seed-{seed:04d}"
    seed_dir = config.run_root() / "seeds" / label
    seed_dir.mkdir(parents=True, exist_ok=True)
    trace_dirs: list[str] = []
    detail: dict[str, Any] = {}
    with activate_soak(session):
        if config.real_request:
            real_dir = seed_dir / "real-request"
            trace_dirs.append(str(real_dir))
            detail["real_request"] = _run_real_request(
                config=config, session=session, run_dir=real_dir
            )
        if config.mailbox:
            mailbox_dir = seed_dir / "mailbox"
            trace_dirs.append(str(mailbox_dir))
            episode = MailboxEpisode(config=config, session=session, run_dir=mailbox_dir)
            try:
                episode.start()
                episode.admit()
                episode.run_steps(schedule)
                episode.finish()
                detail["mailbox"] = {
                    "events_observed": episode.events_observed,
                    "schedule_consumed": schedule.consumed,
                    "executions": len(session.ledger.executions),
                }
            finally:
                try:
                    episode.close()
                except BaseException as error:  # pragma: no cover - teardown guard
                    session.ledger.record_dropped_observation(
                        f"mailbox_teardown:{type(error).__name__}"
                    )
    for request_id, row in sorted(session.ledger.rows.items()):
        # The soak stops driving a row at the end of its episode.  A row that was
        # merely still in flight (no stall was ever observed) is closed by the
        # soak, so it is never judged as a missing terminal; a row that *did* stall
        # stays open and is judged by the cancellation budget.
        if row.terminal_kind is None and row.stalled_at is None:
            session.ledger.close_row_by_soak(
                request_id, note="soak_stopped_driving_at_episode_end"
            )
    violations = session.ledger.check_invariants(bounds)
    soak_material = {
        **session.ledger.soak_material(),
        "seed": seed,
        "events": config.events,
        "schedule_digest": schedule.digest(),
        "weights_digest": config.weights.digest(),
        "bounds_digest": bounds.digest(),
        "forced": [fault.as_document() for fault in config.forced],
        "violations": sorted(
            f"{violation.code}|{violation.subject}" for violation in violations
        ),
    }
    product_material = {
        "seed": seed,
        "execution_counts": session.ledger.execution_counts(),
        "terminal_classes": session.ledger.terminal_classes(),
        # Restart *kinds* and the *set* of named rejections are stable across two
        # runs of one seed; how many times a poll window happened to be empty, or
        # which cursor an owner restart resumed from, is not - so those counts stay
        # in the record and out of the product digest.
        "restart_counts": _label_counts(session.ledger.restarts),
        "named_rejection_codes": sorted(
            {code.split(":", 1)[0] for code in session.ledger.named_rejections}
        ),
        "dropped_observation_kinds": sorted(
            {label.split(":", 1)[0] for label in session.ledger.dropped_observations}
        ),
    }
    result = SeedResult(
        seed=seed,
        label=label,
        schedule_digest=schedule.digest(),
        run_digest=_digest(soak_material),
        product_digest=_digest(product_material),
        violations=violations,
        application_counts=session.ledger.application_counts(),
        execution_counts=session.ledger.execution_counts(),
        terminal_classes=session.ledger.terminal_classes(),
        events=config.events,
        trace_dirs=tuple(trace_dirs),
        replayed=replayed,
        wall_seconds=time.monotonic() - started,
        detail=detail,
    )
    document = dict(result.as_document())
    document["bounds"] = bounds.limits()
    document["schedule"] = schedule.events_document()
    document["ledger"] = {
        "executions": [
            {
                "command_id": record.command_id,
                "host_id": record.host_id,
                "placement_id": record.placement_id,
                "phase": record.phase,
                "token_index": record.token_index,
                "injected": record.injected,
                "at_s": round(record.at, 6),
            }
            for record in session.ledger.executions
        ],
        "rows": [
            {
                "request_id": row.request_id,
                "registered_at_s": round(row.registered_at, 6),
                "last_progress_at_s": round(row.last_progress_at, 6),
                "bound_name": row.bound_name,
                "bound_seconds": row.bound_seconds,
                "last_sequence": row.last_sequence,
                "terminal_kind": row.terminal_kind,
                "terminal_code": row.terminal_code,
                "retired_at_s": None if row.retired_at is None else round(row.retired_at, 6),
                "retired_with_terminal": row.retired_with_terminal,
                "retirement_kind": row.retirement_kind,
                "retirement_note": row.retirement_note,
                "scope": row.scope,
                "bound_origin": row.bound_origin,
                "closed_by_soak_at_s": (
                    None if row.closed_by_soak_at is None else round(row.closed_by_soak_at, 6)
                ),
                "closed_by_soak_note": row.closed_by_soak_note,
                "stalled_at_s": None if row.stalled_at is None else round(row.stalled_at, 6),
                "stall_note": row.stall_note,
            }
            for _, row in sorted(session.ledger.rows.items())
        ],
        "applications": [
            {
                "step": application.step,
                "kind": application.kind,
                "target": application.target,
                "label": application.label,
                "bound_name": application.bound_name,
                "bound_seconds": application.bound_seconds,
            }
            for application in session.ledger.applications
        ],
        "loops": [
            {
                "subject": observation.subject,
                "at_s": round(observation.at, 6),
                "progress": observation.progress,
                "membership_valid": observation.membership_valid,
                "terminal_seen": observation.terminal_seen,
                "note": observation.note,
            }
            for observation in session.ledger.loops
        ],
    }
    document["notes"] = (
        "run_digest covers soak-determined material only (schedule, bounds, "
        "weights, fault application counts, violations).  Wall/monotonic stamps, "
        "arrival counts and execution counts live in this record and the trace: "
        "they are what the soak tests, not what identifies the run."
    )
    document["observations"] = session.ledger.product_material()
    (seed_dir / "seed.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (seed_dir / "summary.txt").write_text(
        _summarise_traces(trace_dirs), encoding="utf-8"
    )
    return result


def _summarise_traces(trace_dirs: Sequence[str]) -> str:
    lines: list[str] = []
    for trace_dir in trace_dirs:
        path = Path(trace_dir)
        if not (path / "run.json").is_file():
            lines.append(f"{path.name}: no trace produced")
            continue
        try:
            loaded = load_run(path)
            summary = summarise(path, reference_host="node-a")
        except TraceError as error:
            lines.append(f"{path.name}: trace unreadable: {error}")
            continue
        lines.append(
            f"{path.name}: events={len(loaded.events)} hosts={len(loaded.header.hosts)}"
        )
        lines.extend(f"  {line}" for line in summary.splitlines())
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class SweepResult:
    config: SoakConfig
    seeds: tuple[SeedResult, ...]
    replay_check: Mapping[str, Any]
    divergence_check: Mapping[str, Any]
    wall_seconds: float

    @property
    def green(self) -> bool:
        return all(result.green for result in self.seeds)

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": SOAK_SCHEMA_VERSION,
            "config": self.config.as_document(),
            "seeds_run": len(self.seeds),
            "events_per_seed": self.config.events,
            "green": self.green,
            "wall_seconds": round(self.wall_seconds, 6),
            "replay_check": dict(self.replay_check),
            "divergence_check": dict(self.divergence_check),
            "seeds": [result.as_document() for result in self.seeds],
            "invariant_codes": list(INVARIANT_CODES),
        }


def run_sweep(config: SoakConfig) -> SweepResult:
    """Run every configured seed, then prove the replay and divergence claims."""

    started = time.monotonic()
    results = [run_seed(config, seed) for seed in config.seeds]
    replay_check: dict[str, Any] = {"performed": False}
    divergence_check: dict[str, Any] = {"performed": False}
    if results:
        first = results[0]
        replay = run_seed(config, first.seed, label="replay", replayed=True)
        replay_check = {
            "performed": True,
            "seed": first.seed,
            "first_digest": first.run_digest,
            "replay_digest": replay.run_digest,
            "identical": first.run_digest == replay.run_digest,
            "product_digest_identical": first.product_digest == replay.product_digest,
            "replay_command": replay.replay_command(),
        }
        other_seed = first.seed + 1_000_003
        other = run_seed(config, other_seed, label="divergence", replayed=True)
        divergence_check = {
            "performed": True,
            "seed": other_seed,
            "digest": other.run_digest,
            "differs_from_first": other.run_digest != first.run_digest,
        }
    return SweepResult(
        config=config,
        seeds=tuple(results),
        replay_check=replay_check,
        divergence_check=divergence_check,
        wall_seconds=time.monotonic() - started,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_forced(values: Sequence[str]) -> tuple[ForcedFault, ...]:
    faults: list[ForcedFault] = []
    for value in values:
        kind_text, _, step_text = value.partition("@")
        try:
            kind = FaultKind(kind_text)
        except ValueError as error:
            raise SoakError(f"soak_unknown_forced_fault:{kind_text}") from error
        if kind not in FALSIFICATION_FAULTS:
            raise SoakError(f"soak_forced_fault_not_a_falsification_fault:{kind}")
        faults.append(ForcedFault(kind=kind, step=int(step_text) if step_text else None))
    return tuple(faults)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mycelium_testbed.soak",
        description=(
            "Seeded control-plane fault-injection soak over the real mailbox "
            "enqueue/poll/ack/result path (packet wp2-3)."
        ),
    )
    parser.add_argument("--seeds", type=int, default=1, help="number of seeds to sweep")
    parser.add_argument("--seed", type=int, default=None, help="run exactly this seed")
    parser.add_argument("--events", type=int, default=200, help="mailbox events per seed")
    parser.add_argument("--run-id", default=DEFAULT_RUN_ROOT_NAME)
    parser.add_argument("--out", default=None, help="runs root (default: runs/wp2-3)")
    parser.add_argument(
        "--native-binary",
        default=None,
        help="iroh sidecar binary (default: the harness's own BINARY path)",
    )
    parser.add_argument(
        "--no-real-request",
        action="store_true",
        help="skip the canonical harness.run_complete_request episode",
    )
    parser.add_argument(
        "--mailbox-only",
        action="store_true",
        help="fingerprint the schedule only, without either episode",
    )
    parser.add_argument("--max-sessions", type=int, default=2)
    parser.add_argument(
        "--force-fault",
        action="append",
        default=[],
        metavar="KIND[@STEP]",
        help="falsification fault, repeatable (e.g. double_execution@5)",
    )
    parser.add_argument("--time-scale", type=float, default=1.0)
    parser.add_argument("--fsync", action="store_true")
    parser.add_argument("--json", action="store_true", help="also print the sweep document")
    parser.add_argument("--plan", action="store_true", help="print schedule digests only")
    return parser


def _default_binary() -> Path:
    return Path(load_harness().BINARY)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.seed is not None:
        seeds: tuple[int, ...] = (int(args.seed),)
    else:
        if args.seeds < 1:
            parser.error("--seeds must be at least 1")
        seeds = tuple(range(1, int(args.seeds) + 1))
    binary = Path(args.native_binary) if args.native_binary else _default_binary()
    config = SoakConfig(
        seeds=seeds,
        events=int(args.events),
        run_id=str(args.run_id),
        out_root=Path(args.out) if args.out else None,
        native_binary=binary,
        forced=parse_forced(args.force_fault),
        real_request=not args.no_real_request,
        mailbox=not args.mailbox_only,
        max_sessions=int(args.max_sessions),
        time_scale=float(args.time_scale),
        fsync=bool(args.fsync),
    )
    if args.plan:
        for seed in seeds:
            schedule = FaultSchedule(
                seed=seed,
                events=config.events,
                weights=config.weights,
                forced=config.forced,
            )
            print(
                f"seed={seed} schedule_digest={schedule.digest()} events={config.events}"
            )
        return 0
    sweep = run_sweep(config)
    run_root = config.run_root()
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "sweep.json").write_text(
        json.dumps(sweep.as_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = [
        f"soak {SOAK_SCHEMA_VERSION} run_id={config.run_id}",
        f"seeds_run={len(sweep.seeds)} events_per_seed={config.events} "
        f"wall_seconds={sweep.wall_seconds:.2f}",
        f"invariant_codes={list(INVARIANT_CODES)}",
        f"bounds_digest={config.resolved_bounds().digest()}",
        f"replay_check={json.dumps(sweep.replay_check, sort_keys=True)}",
        f"divergence_check={json.dumps(sweep.divergence_check, sort_keys=True)}",
    ]
    for result in sweep.seeds:
        report.append(
            f"seed={result.seed} digest={result.run_digest} "
            f"product_digest={result.product_digest} green={result.green} "
            f"codes={list(result.invariant_codes_fired)} wall={result.wall_seconds:.2f}s"
        )
        for violation in result.violations:
            report.append(
                f"  violation {violation.code} subject={violation.subject} "
                f"bound={violation.bound_name}={violation.bound_value} "
                f"detail={violation.detail}"
            )
    report.append(f"run_root={run_root}")
    text = "\n".join(report) + "\n"
    (run_root / "report.txt").write_text(text, encoding="utf-8")
    print(text)
    if args.json:
        print(json.dumps(sweep.as_document(), sort_keys=True))
    return 0 if sweep.green else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
