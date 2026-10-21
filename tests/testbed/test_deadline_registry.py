# SPDX-License-Identifier: AGPL-3.0-only
"""wp2-1: the deadline/budget registry and its composition invariants.

Covers (PLAN 5.2 item 1):

* the **round-50 RED case**: the preview.50 composition (peer advertises
  2000 ms, snapshot attempt / fallback reserve 500 ms, total budget 2000 ms ->
  route fanout window 1500 ms) must be reported as exactly
  ``peer_advertised_cleanup_bound_exceeds_route_fanout_window``;
* the **wp5-d9 closure** of that defect, **derivation corrected by merge
  a13-resolve**: the shipped advertised bound is ``window - transport margin -
  slack`` (2_000 - 500 = 1_500 ms window, - 200 ms margin = 1_300 ms admitted,
  - 100 ms slack = 1_200 ms advertised), ``check_composition()`` reports
  nothing, the shipped advertisement is admitted by the route's own
  ``admit_peer_cleanup_bound`` (the node must not refuse itself), the node
  payload reads the registry symbol at construction time (no copy), and the
  canonical fixture/producer carry the same registry value;
* the **wp5-bind-residual closure** of the remaining pending contract-pinned
  bindings: every consumer in ``PENDING_CONTRACT_PINNED_BINDINGS`` is assigned
  from its registry symbol (route.py's other cleanup constants,
  ``mycelium_request_gateway/service.py``, ``mycelium_live/local_preparer.py``),
  so mutating the registry symbol is followed by the consumer instead of
  leaving a second copy behind;
* an honest, **deliberately pinned** statement of today's shipped composition;
* registry hygiene (derivation, units, names);
* a **no-drift** guard: the registry values equal the historical literals that
  shipped in the consumer modules (written as literals below on purpose; the
  one value corrected on purpose - the advertised bound - carries its own note);
* the consumer constants stay importable **and monkeypatchable by dotted string
  path**, and the bound ones actually read the registry.
"""
from __future__ import annotations

import ast
import importlib
import json
import re
import sys
import tempfile
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import mycelium_deadlines as deadlines

ROOT = Path(__file__).resolve().parents[2]

#: The violation set shipped today: none. The round-50 defect (PLAN D9) was
#: closed by packet wp5-d9, and its derivation corrected by merge a13-resolve:
#: the node advertises the admitted maximum less one slack (1200 ms = 1500 ms
#: window - 200 ms transport margin - 100 ms slack) instead of the shipped
#: 2000 ms literal, and advertising the whole 1500 ms window would make the
#: route's own admission predicate refuse the node at startup. Any violation
#: appearing here again is a regression.
PINNED_SHIPPED_VIOLATED_CODES: frozenset[str] = frozenset()

#: Historical literals as they shipped, transcribed by hand from the consumer
#: modules (file:line cited in the registry's ``derivation``). Deliberately NOT
#: read from the registry: this is the independent side of the no-drift check.
HISTORICAL_LITERALS: dict[str, float | int] = {
    "phone_request_execution_max_ms": 3_600_000,
    "phone_thermal_cooldown_seconds": 300.0,
    "cancel_and_cleanup_budget_seconds": 8.0,
    "cleanup_fallback_reserve_seconds": 3.5,
    "cleanup_snapshot_attempt_seconds": 3.5,
    "cleanup_inline_receipt_grace_seconds": 0.5,
    "cleanup_receipt_poll_seconds": 0.1,
    "cleanup_reconciliation_horizon_seconds": 60.0,
    "cleanup_reconciliation_workers": 4,
    "idle_keepalive_probe_deadline_seconds": 0.5,
    # Corrected twice on purpose.  (wp5-d9 / a13-resolve made the shipped
    # 2_000 ms fit inside the then window: window - margin - slack = 1_200 ms.)
    # RE-DERIVED 2026-09-20 by owner decision from the MEASURED two-Mac cleanup
    # (1.83 s / 3.18 s): the 2.0 s budget became 8.0 s, the window 4.5 s, and
    # this value 4_500 - 200 - 100 = 4_200 ms -- which clears the measured worst
    # case by 32 % instead of hiding it behind a window-shaped number.
    "peer_advertised_maximum_total_cleanup_ms": 4_200,
    # a13-resolve additions: the a13 lane's MEASURED cleanups, registered so
    # the imported lane carries no second copy of a number this registry owns.
    # The slice covers a real relayed snapshot round trip (0.875-1.066 s
    # measured, and the previous 0.5 s slice expired unclaimed); the margin is
    # command delivery plus receipt return, the part of the window that is
    # transport and not cleanup; the slack is the headroom the advertised bound
    # keeps below the predicate's admitted maximum.
    "cleanup_snapshot_slice_seconds": 4.0,
    "peer_cleanup_bound_transport_margin_ms": 200,
    "peer_advertised_cleanup_bound_slack_ms": 100,
    "gateway_sse_disconnect_poll_seconds": 0.1,
    "gateway_producer_stall_poll_seconds": 0.05,
    "route_health_probe_deadline_seconds": 0.5,
    "command_interruption_and_cleanup_max_ms": 8_000,
    "command_default_cooperative_step_ms": 100,
    "gateway_session_eviction_grace_seconds": 0.05,
    "gateway_session_silent_subscriber_stall_seconds": 0.25,
    "liveness_active_failure_detection_ms": 2_000,
    "liveness_idle_keepalive_ms": 5_000,
    "liveness_quarantine_stale_ms": 15_000,
    "member_transport_grant_ttl_ms": 900_000,
    "availability_reconcile_min_seconds": 30.0,
    "availability_reconcile_max_seconds": 1_800.0,
    "node_launch_handshake_seconds": 2.0,
    "node_command_timeout_seconds": 900.0,
    "node_session_timeout_seconds": 930.0,
    "node_identity_create_retries": 100,
    "node_identity_create_retry_seconds": 0.005,
    "artifact_request_clock_skew_ms": 5_000,
    "stage_pack_min_authority_ttl_ms": 900_000,
    "stage_pack_max_authority_ttl_ms": 21_600_000,
    "qualification_max_age_seconds": 3_600.0,
    "requalification_interval_seconds": 3_300.0,
    # wp2-6 addition (a new registered value, not a shipped literal): the
    # connect-phase bound of one peer delivery. Pinned here on purpose - the
    # registry must not tune it silently, and it must stay strictly inside the
    # route's cleanup fanout window.
    "peer_connect_deadline_seconds": 1.0,
}

#: Constants the deadline bindings packets bound to the registry, by file. Each
#: must be assigned from ``mycelium_deadlines.<NAME>`` (module level, and for
#: class attributes in the class body, so monkeypatching the name on the module
#: or the class still replaces exactly what the call sites read).
BOUND_SOURCES: dict[str, tuple[str, ...]] = {
    "mycelium_iroh_sidecar/client.py": ("PEER_CONNECT_DEADLINE_SECONDS",),
    "mycelium_live/member_transport.py": (
        "_GRANT_TTL_MS",
        "_AVAILABILITY_RECONCILE_MIN_SECONDS",
        "_AVAILABILITY_RECONCILE_MAX_SECONDS",
    ),
    # wp5-d9: the two constants that form the route's cleanup fanout window.
    # wp5-bind-residual: the route's remaining cleanup / keepalive constants.
    # a13-resolve: the a13 lane's measured snapshot slice and peer-bound
    # transport margin, re-bound from route-local literals to the registry.
    # wp5-cleanup-levers: `_CLEANUP_SNAPSHOT_ATTEMPT_SECONDS` is no longer here -
    # it was bound and consumed nowhere while the live slice is 1.1 s, so the
    # record is explicitly non-binding (see NON_BINDING_DEADLINES).
    "mycelium_live/route.py": (
        "_IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS",
        "_CLEANUP_RECEIPT_POLL_SECONDS",
        "_CLEANUP_SNAPSHOT_SLICE_SECONDS",
        "_CLEANUP_FALLBACK_RESERVE_SECONDS",
        "_CLEANUP_INLINE_RECEIPT_GRACE_SECONDS",
        "_CLEANUP_RECONCILIATION_HORIZON_SECONDS",
        "_CLEANUP_RECONCILIATION_WORKERS",
        "PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS",
        # 2026-09-20 cleanup-budget re-derivation: the route mints its
        # cancellation/cleanup deadlines from the registry record.
        "_MAX_CANCELLATION_AND_CLEANUP_SECONDS",
    ),
    "mycelium_live/router_port.py": ("_MAX_CANCELLATION_AND_CLEANUP_SECONDS",),
    # 2026-09-20: the controller ceiling is the same window, in ms for the node
    # protocol, and it is what refuses a routed cleanup deadline mid-request.
    "mycelium_live/command_controller.py": ("MAX_INTERRUPTION_AND_CLEANUP_MS",),
    # wp5-bind-residual: the stage-pack authority TTL bounds.
    "mycelium_live/local_preparer.py": (
        "_STAGE_PACK_MIN_AUTHORITY_TTL_MS",
        "_STAGE_PACK_MAX_AUTHORITY_TTL_MS",
    ),
    # wp5-bind-residual: the gateway's cancellation budget and the two bounded
    # session graces (class attributes on ``RequestGatewayService``).
    "mycelium_request_gateway/service.py": (
        "_MAX_CANCELLATION_AND_CLEANUP_SECONDS",
        "RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS",
        "RequestGatewayService.SESSION_SILENT_SUBSCRIBER_STALL_SECONDS",
    ),
    "mycelium_node/process.py": ("_LAUNCH_HANDSHAKE_SECONDS",),
    # a13-resolve: the a13 lane's NAMED cleanup bound, assigned from the same
    # registry symbol the node payload below reads.
    "physical_inference_node.py": ("MAXIMUM_TOTAL_CLEANUP_MS",),
}

#: Packet wp5-bind-residual: the consumers that were listed in
#: ``PENDING_CONTRACT_PINNED_BINDINGS`` and are now bound.  ``(source path,
#: attribute path, registry record name)`` - the record name resolves through
#: ``deadlines.deadline()`` and its uppercase symbol is the registry symbol the
#: consumer must be assigned from.  The anti-drift test below executes the real
#: module source under a registry symbol mutated to a sentinel and asserts the
#: consumer follows it - the exact property a stale copy cannot satisfy.
RESIDUAL_ANTI_DRIFT_CONSUMERS: tuple[tuple[str, str, str], ...] = (
    (
        "mycelium_live/route.py",
        "_IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS",
        "idle_keepalive_probe_deadline_seconds",
    ),
    (
        "mycelium_live/route.py",
        "_CLEANUP_SNAPSHOT_SLICE_SECONDS",
        "cleanup_snapshot_slice_seconds",
    ),
    (
        "mycelium_live/route.py",
        "PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS",
        "peer_cleanup_bound_transport_margin_ms",
    ),
    (
        "mycelium_live/route.py",
        "_CLEANUP_RECEIPT_POLL_SECONDS",
        "cleanup_receipt_poll_seconds",
    ),
    (
        "mycelium_live/route.py",
        "_CLEANUP_INLINE_RECEIPT_GRACE_SECONDS",
        "cleanup_inline_receipt_grace_seconds",
    ),
    (
        "mycelium_live/route.py",
        "_CLEANUP_RECONCILIATION_HORIZON_SECONDS",
        "cleanup_reconciliation_horizon_seconds",
    ),
    (
        "mycelium_live/route.py",
        "_CLEANUP_RECONCILIATION_WORKERS",
        "cleanup_reconciliation_workers",
    ),
    (
        "mycelium_live/local_preparer.py",
        "_STAGE_PACK_MIN_AUTHORITY_TTL_MS",
        "stage_pack_min_authority_ttl_ms",
    ),
    (
        "mycelium_live/local_preparer.py",
        "_STAGE_PACK_MAX_AUTHORITY_TTL_MS",
        "stage_pack_max_authority_ttl_ms",
    ),
    (
        "mycelium_request_gateway/service.py",
        "_MAX_CANCELLATION_AND_CLEANUP_SECONDS",
        "cancel_and_cleanup_budget_seconds",
    ),
    (
        "mycelium_request_gateway/service.py",
        "RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS",
        "gateway_session_eviction_grace_seconds",
    ),
    (
        "mycelium_request_gateway/service.py",
        "RequestGatewayService.SESSION_SILENT_SUBSCRIBER_STALL_SECONDS",
        "gateway_session_silent_subscriber_stall_seconds",
    ),
    (
        "physical_inference_node.py",
        "MAXIMUM_TOTAL_CLEANUP_MS",
        "peer_advertised_maximum_total_cleanup_ms",
    ),
)

#: Dotted names the existing suites patch by string
#: (``tests/live/test_a4_ownership.py:594,667,942``). They must keep working.
MONKEYPATCHED_DOTTED_PATHS: tuple[str, ...] = (
    "mycelium_live.route._CLEANUP_INLINE_RECEIPT_GRACE_SECONDS",
    "mycelium_live.route._CLEANUP_RECONCILIATION_HORIZON_SECONDS",
)

#: Consumers whose constant is expressed in milliseconds while the registry
#: record is in seconds - a unit conversion, not a different value.
CONSUMER_MS_SCALE: dict[str, int] = {
    "mycelium_live.health.LIVE_QUALIFICATION_REFRESH_AFTER_MS": 1_000,
}


def _resolve(dotted: str) -> Any:
    """Resolve ``a.b.c`` to an object, walking attributes after the module part."""

    parts = dotted.split(".")
    for index in range(len(parts), 0, -1):
        module_name = ".".join(parts[:index])
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        resolved: Any = module
        for attribute in parts[index:]:
            resolved = getattr(resolved, attribute)
        return resolved
    raise AssertionError(f"cannot resolve dotted name: {dotted}")


def _module_level_assignments(relative_path: str) -> dict[str, str]:
    """Assignments a reader can patch: module level, plus class attributes.

    A module-level ``NAME = value`` assignment is recorded under ``NAME``; an
    assignment in a class body is recorded as ``ClassName.NAME`` so a bound
    class attribute (``RequestGatewayService.SESSION_EVICTION_GRACE_SECONDS``)
    is asserted textually exactly like a module global.
    """

    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    assigned: dict[str, str] = {}

    def record(items: list[ast.stmt], prefix: str) -> None:
        for node in items:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                assigned[f"{prefix}{node.targets[0].id}"] = ast.unparse(node.value)

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            record(node.body, f"{node.name}.")
            continue
        record([node], "")
    return assigned


# --------------------------------------------------------------------------- #
# The round-50 RED case (PLAN D9) and the pinned shipped composition.
# --------------------------------------------------------------------------- #


def test_round_50_preview_50_composition_is_red() -> None:
    """preview.50: 2000 ms advertised, 500 ms snapshot attempt, 2000 ms budget."""

    total_budget_seconds = 2.0
    snapshot_attempt_seconds = 0.5
    advertised_cleanup_ms = 2_000
    assert snapshot_attempt_seconds * 1_000 == 500

    window_ms = deadlines.derive_route_cleanup_fanout_window_ms(
        budget_seconds=total_budget_seconds,
        fallback_reserve_seconds=snapshot_attempt_seconds,
    )
    assert window_ms == 1_500

    violated = deadlines.check_composition(
        advertised_cleanup_ms=advertised_cleanup_ms,
        total_budget_seconds=total_budget_seconds,
        fallback_reserve_seconds=snapshot_attempt_seconds,
    )
    assert violated == [
        deadlines.REASON_PEER_ADVERTISED_CLEANUP_BOUND_EXCEEDS_ROUTE_FANOUT_WINDOW
    ]


def test_shipped_composition_violation_set_is_pinned() -> None:
    """Today's shipped values satisfy the whole composition (D9, closed).

    Packet wp5-d9 closed the round-50 defect, so the pinned violation set is
    empty; the numbers below restate the shipped composition so the pin is
    self-evident, and a reappearing violation fails this test.
    """

    shipped = deadlines.check_composition()
    assert set(shipped) == PINNED_SHIPPED_VIOLATED_CODES == set()
    assert shipped == []

    # Restate the shipped composition in numbers so the pin is self-evident.
    assert deadlines.advertised_maximum_total_cleanup_ms == 4_200
    assert deadlines.route_cleanup_fanout_window_ms == 4_500
    assert (
        deadlines.advertised_maximum_total_cleanup_ms
        <= deadlines.route_cleanup_fanout_window_ms
    )


def test_clean_composition_reports_no_violation() -> None:
    assert (
        deadlines.check_composition(
            advertised_cleanup_ms=1_500,
            fanout_window_ms=1_500,
        )
        == []
    )


def test_each_invariant_reports_its_own_reason_code() -> None:
    assert deadlines.check_peer_advertised_cleanup_bound(
        advertised_cleanup_ms=1_501,
        fanout_window_ms=1_500,
    ) == deadlines.REASON_PEER_ADVERTISED_CLEANUP_BOUND_EXCEEDS_ROUTE_FANOUT_WINDOW
    assert (
        deadlines.check_peer_advertised_cleanup_bound(
            advertised_cleanup_ms=1_500,
            fanout_window_ms=1_500,
        )
        is None
    )

    assert deadlines.check_cooperative_step_bound(
        cooperative_step_ms=2_500,
        interruption_and_cleanup_ms=2_000,
    ) == deadlines.REASON_COOPERATIVE_STEP_EXCEEDS_INTERRUPTION_AND_CLEANUP
    assert (
        deadlines.check_cooperative_step_bound(
            cooperative_step_ms=2_000,
            interruption_and_cleanup_ms=2_000,
        )
        is None
    )

    assert deadlines.check_qualification_age_bound(
        qualification_max_age_seconds=3_300,
        requalification_interval_seconds=3_300,
    ) == (
        deadlines.REASON_QUALIFICATION_MAX_AGE_NOT_GREATER_THAN_REQUALIFICATION_INTERVAL
    )
    assert (
        deadlines.check_qualification_age_bound(
            qualification_max_age_seconds=3_300.001,
            requalification_interval_seconds=3_300,
        )
        is None
    )


def test_check_composition_returns_only_named_reason_codes() -> None:
    for code in deadlines.COMPOSITION_INVARIANTS:
        assert code and code.islower() and " " not in code
    assert set(deadlines.COMPOSITION_INVARIANTS) == {
        deadlines.REASON_PEER_ADVERTISED_CLEANUP_BOUND_EXCEEDS_ROUTE_FANOUT_WINDOW,
        deadlines.REASON_COOPERATIVE_STEP_EXCEEDS_INTERRUPTION_AND_CLEANUP,
        deadlines.REASON_QUALIFICATION_MAX_AGE_NOT_GREATER_THAN_REQUALIFICATION_INTERVAL,
    }
    # Never a bare False/True: a violated invariant is always a named code.
    for violated in deadlines.check_composition():
        assert violated in deadlines.COMPOSITION_INVARIANTS


def test_invalid_composition_values_fail_closed() -> None:
    with pytest.raises(ValueError):
        deadlines.check_composition(advertised_cleanup_ms="2000")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        deadlines.check_composition(fanout_window_ms=True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Derived quantities (PLAN 5.2 item 1 / D9 symbols).
# --------------------------------------------------------------------------- #


def test_derived_quantities_are_the_published_symbols() -> None:
    assert deadlines.route_cleanup_fanout_window_seconds == 4.5
    assert deadlines.route_cleanup_fanout_window_ms == 4_500
    # wp5-d9: the advertised bound is derived from the window, not a literal.
    assert deadlines.advertised_maximum_total_cleanup_ms == 4_200
    assert deadlines.advertised_maximum_total_cleanup_ms == (
        deadlines.derive_advertised_maximum_total_cleanup_ms()
    )
    # a13-resolve: the derivation keeps the advertised bound inside the admitted
    # maximum the route's admission predicate applies (window - margin), with
    # one explicit slack on top.
    assert deadlines.advertised_maximum_total_cleanup_ms == (
        deadlines.route_cleanup_fanout_window_ms
        - deadlines.PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS
        - deadlines.PEER_ADVERTISED_CLEANUP_BOUND_SLACK_MS
    )
    assert deadlines.derive_route_cleanup_fanout_window_seconds() == (
        deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS
        - deadlines.CLEANUP_FALLBACK_RESERVE_SECONDS
    )
    with pytest.raises(ValueError):
        deadlines.derive_route_cleanup_fanout_window_ms(
            budget_seconds=2.0,
            fallback_reserve_seconds=0.3333,
        )
    with pytest.raises(ValueError):
        deadlines.derive_advertised_maximum_total_cleanup_ms(
            fanout_window_ms=0.0,
        )
    with pytest.raises(ValueError):
        deadlines.derive_advertised_maximum_total_cleanup_ms(
            fanout_window_ms=1_500.5,
        )
    with pytest.raises(ValueError):
        deadlines.derive_advertised_maximum_total_cleanup_ms(
            fanout_window_ms="1500",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        deadlines.derive_advertised_maximum_total_cleanup_ms(
            fanout_window_ms=1_500,
            transport_margin_ms=-1,
        )
    with pytest.raises(ValueError):
        deadlines.derive_advertised_maximum_total_cleanup_ms(
            fanout_window_ms=200,
            transport_margin_ms=200,
            slack_ms=100,
        )


# --------------------------------------------------------------------------- #
# Registry hygiene.
# --------------------------------------------------------------------------- #


def test_peer_connect_deadline_sits_strictly_inside_the_route_fanout_window() -> None:
    """wp2-6: one vanished peer must not consume the route's fanout window.

    The RCA (`rca/wp2-iroh-delivery.md`, defect 2) requires a bounded, *named*
    connect failure; the packet requires its bound to stay strictly below the
    route's 1.5 s cleanup fanout window so a dead peer cannot eat the budget the
    route needs to fan cancellation out to the peers that are still alive.
    """

    record = deadlines.deadline("peer_connect_deadline_seconds")
    value = record.value

    assert record.unit == "seconds"
    assert value == 1.0
    assert 0 < value < deadlines.route_cleanup_fanout_window_seconds == 4.5
    # The window it must fit inside is itself derived, not asserted by hand.
    assert deadlines.route_cleanup_fanout_window_seconds == (
        deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS
        - deadlines.CLEANUP_FALLBACK_RESERVE_SECONDS
    )
    # And it is smaller than the shipped Python delivery budget it protects:
    # a dead peer must fail before the hop expires, not after it.
    assert value < 5.0


def test_every_registry_entry_is_complete_and_typed() -> None:
    names = [record.name for record in deadlines.DEADLINES]
    assert len(names) == len(set(names))
    assert set(names) == set(HISTORICAL_LITERALS)
    for record in deadlines.DEADLINES:
        assert record.derivation.strip(), record.name
        assert record.unit in deadlines.UNITS, record.name
        assert record.owner.strip(), record.name
        assert record.source.strip(), record.name
        # The record's value is importable under name.upper().
        assert getattr(deadlines, record.name.upper()) == record.value
        assert deadlines.deadline(record.name) is record
    with pytest.raises(KeyError):
        deadlines.deadline("not_a_deadline")


def test_consumer_bindings_reference_registered_deadlines() -> None:
    assert deadlines.PENDING_BINDINGS == tuple(
        sorted(
            deadlines.PENDING_MEMBER_RUNTIME_CLOSURE_BINDINGS
            + deadlines.PENDING_CONTRACT_PINNED_BINDINGS
            + deadlines.PENDING_OUT_OF_SCOPE_BINDINGS
        )
    )
    assert deadlines.CONSUMER_BINDINGS == tuple(
        sorted(deadlines.BOUND_CONSUMER_BINDINGS + deadlines.PENDING_BINDINGS)
    )
    bound = set(deadlines.BOUND_CONSUMER_BINDINGS)
    closure = set(deadlines.PENDING_MEMBER_RUNTIME_CLOSURE_BINDINGS)
    pinned = set(deadlines.PENDING_CONTRACT_PINNED_BINDINGS)
    out_of_scope = set(deadlines.PENDING_OUT_OF_SCOPE_BINDINGS)
    groups = (bound, closure, pinned, out_of_scope)
    for index, first in enumerate(groups):
        for second in groups[index + 1:]:
            assert not first & second
    for dotted, name in deadlines.CONSUMER_BINDINGS:
        assert name in deadlines.DEADLINES_BY_NAME, dotted
    # Every registry record that replaces a named consumer symbol is listed.
    consumers = {name for _, name in deadlines.CONSUMER_BINDINGS}
    assert consumers == set(deadlines.DEADLINES_BY_NAME) - set(
        deadlines.NON_BINDING_DEADLINES
    )
    for name in deadlines.NON_BINDING_DEADLINES:
        assert name in deadlines.DEADLINES_BY_NAME, name


# --------------------------------------------------------------------------- #
# No-drift: registry values == the historical literals, and the consumers agree.
# --------------------------------------------------------------------------- #


def test_registry_values_have_not_drifted_from_the_shipped_literals() -> None:
    for name, literal in HISTORICAL_LITERALS.items():
        record = deadlines.deadline(name)
        assert record.value == literal, name
        assert type(record.value) is type(literal), name
        assert getattr(deadlines, name.upper()) == literal, name


def test_every_consumer_constant_equals_its_registry_value() -> None:
    for dotted, name in deadlines.CONSUMER_BINDINGS:
        expected = deadlines.deadline(name).value
        observed = _resolve(dotted)
        assert observed == expected * CONSUMER_MS_SCALE.get(dotted, 1), dotted


def test_bound_constants_are_assigned_from_the_registry() -> None:
    for relative_path, constant_names in BOUND_SOURCES.items():
        assigned = _module_level_assignments(relative_path)
        for name in constant_names:
            assert name in assigned, f"{relative_path}:{name} missing"
            expression = assigned[name]
            assert expression.startswith("mycelium_deadlines."), (
                f"{relative_path}:{name} is not bound to the registry: {expression}"
            )
            assert re.fullmatch(
                r"mycelium_deadlines\.[A-Z0-9_]+", expression
            ), expression


# --------------------------------------------------------------------------- #
# wp5-bind-residual: the remaining contract-pinned consumers follow the registry
# --------------------------------------------------------------------------- #


def _resolve_attribute(root: Any, attribute_path: str) -> Any:
    resolved = root
    for attribute in attribute_path.split("."):
        resolved = getattr(resolved, attribute)
    return resolved


@contextmanager
def _registry_symbol_mutated(name: str, value: float | int) -> Iterator[None]:
    """Temporarily set a registry symbol to ``value`` (restored afterwards)."""

    original = getattr(deadlines, name)
    setattr(deadlines, name, value)
    try:
        yield
    finally:
        setattr(deadlines, name, original)


@contextmanager
def _executed_consumer_module(
    relative_path: str, *, source: str | None = None
) -> Iterator[Any]:
    """Execute a consumer module's real source under a synthetic module name.

    This is what ``import mycelium_live.route`` runs, minus the ``sys.modules``
    cache: the module body executes against the *live* registry, so a bound
    constant reads the registry symbol at execution time and a stale literal
    does not.  Relative imports in the executed module keep resolving through
    its real package (``__package__`` is set to the source file's package).
    """

    text = (
        source
        if source is not None
        else (ROOT / relative_path).read_text(encoding="utf-8")
    )
    dotted = Path(relative_path).with_suffix("").as_posix().replace("/", ".")
    package = dotted.rsplit(".", 1)[0]
    stem = Path(relative_path).stem
    synthetic_name = f"{package}._wp5_bind_residual_probe_{stem}"
    module = types.ModuleType(synthetic_name)
    module.__package__ = package
    module.__file__ = str(ROOT / relative_path)
    previous = sys.modules.get(synthetic_name)
    sys.modules[synthetic_name] = module
    try:
        exec(compile(text, module.__file__, "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        if previous is None:
            sys.modules.pop(synthetic_name, None)
        else:
            sys.modules[synthetic_name] = previous


@pytest.mark.parametrize(
    "relative_path, attribute_path, registry_name",
    RESIDUAL_ANTI_DRIFT_CONSUMERS,
    ids=[f"{path}:{attribute}" for path, attribute, _ in RESIDUAL_ANTI_DRIFT_CONSUMERS],
)
def test_residual_bound_consumer_follows_a_registry_mutation(
    relative_path: str, attribute_path: str, registry_name: str
) -> None:
    """wp5-bind-residual anti-drift: the consumer reads the registry symbol.

    The registry symbol is mutated to a sentinel *before* the consumer's source
    executes, and the consumer must report the sentinel.  A consumer that keeps
    its own copy reports the shipped value instead and fails here.
    """

    shipped = deadlines.deadline(registry_name).value
    sentinel = shipped * 3 + 1
    assert sentinel != shipped

    with _registry_symbol_mutated(registry_name.upper(), sentinel):
        with _executed_consumer_module(relative_path) as module:
            observed = _resolve_attribute(module, attribute_path)

    assert observed == sentinel, (
        f"{relative_path}:{attribute_path} did not follow "
        f"mycelium_deadlines.{registry_name.upper()} "
        f"(observed {observed!r}, sentinel {sentinel!r}, shipped {shipped!r})"
    )
    # The registry is the owner: the shipped value equals the record's value.
    assert getattr(deadlines, registry_name.upper()) == shipped


def test_residual_antidrift_assertions_detect_a_stale_copy() -> None:
    """Assertion liveness: a reverted literal is detected, so the guard is live.

    For every residual consumer the binding expression is replaced by the
    historical literal it replaced and executed under a mutated registry; the
    same follow-check then observes the literal (not the sentinel) - which is
    exactly how it fails when a second copy reappears.
    """

    for relative_path, attribute_path, registry_name in RESIDUAL_ANTI_DRIFT_CONSUMERS:
        shipped_source = (ROOT / relative_path).read_text(encoding="utf-8")
        attribute_name = attribute_path.split(".")[-1]
        symbol = registry_name.upper()
        literal = HISTORICAL_LITERALS[registry_name]
        binding = f"{attribute_name} = mycelium_deadlines.{symbol}"
        assert binding in shipped_source, (relative_path, binding)
        stale_source = shipped_source.replace(binding, f"{attribute_name} = {literal!r}")

        sentinel = literal * 3 + 1
        with _registry_symbol_mutated(symbol, sentinel):
            with _executed_consumer_module(
                relative_path, source=stale_source
            ) as module:
                observed = _resolve_attribute(module, attribute_path)
        assert observed == literal, (relative_path, attribute_path, observed)
        assert observed != sentinel, (relative_path, attribute_path, observed)


def test_advertised_bound_is_the_registry_value_and_matches_the_canonical_producer() -> None:
    """PLAN D9's validated side: the node advertises the registry symbol.

    Packet wp5-d9 bound the payload (``physical_inference_node.py``) and the
    canonical producer (``scripts/generate_contract_fixtures.py``) to
    ``mycelium_deadlines.advertised_maximum_total_cleanup_ms`` and refreshed the
    manifest pins in the same change. This AST check keeps the binding textual -
    a literal reappearing in either file fails here - while the executed-payload
    and fixture checks above prove the value actually flows from the registry.
    """

    tree = ast.parse((ROOT / "physical_inference_node.py").read_text(encoding="utf-8"))
    advertised: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "maximum_total_cleanup_ms":
                advertised.append(ast.unparse(value))
    assert advertised, "physical_inference_node.py lost the advertised cleanup bound"
    for expression in advertised:
        if expression.startswith("mycelium_deadlines."):
            assert expression == (
                "mycelium_deadlines.advertised_maximum_total_cleanup_ms"
            ), expression
            continue
        assert ast.literal_eval(expression) == (
            deadlines.advertised_maximum_total_cleanup_ms
        ), expression

    # The canonical fixture producer reads the same registry symbol (never a
    # hand-edited number), and the registry value is the 1500 ms D9 window.
    producer = (ROOT / "scripts" / "generate_contract_fixtures.py").read_text(
        encoding="utf-8"
    )
    pinned = re.findall(
        r'"maximum_total_cleanup_ms":\s*([A-Za-z_][A-Za-z0-9_.]*)', producer
    )
    assert pinned == ["advertised_maximum_total_cleanup_ms"], pinned
    assert deadlines.advertised_maximum_total_cleanup_ms == 4_200


def test_registry_only_inline_literals_still_hold_their_registry_values() -> None:
    """The records with no named consumer symbol are drifted against the code.

    These values live as inline literals (or a payload key) in modules this
    packet may not rewrite, so the registry is guarded here: if the literal in
    the module changes, this test fails rather than the registry silently
    disagreeing with the shipped code.
    """

    expectations = (
        (
            "mycelium_request_gateway/asgi.py",
            r"subscription\.next_event, timeout=([0-9.]+)",
            "gateway_sse_disconnect_poll_seconds",
        ),
        (
            "mycelium_request_gateway/service.py",
            r"session\.condition\.wait\(timeout=([0-9.]+)\)",
            "gateway_producer_stall_poll_seconds",
        ),
        (
            "mycelium_live/route.py",
            r"deadline_monotonic_s=observed_at_s \+ ([0-9.]+)",
            "route_health_probe_deadline_seconds",
        ),
    )
    for relative, pattern, name in expectations:
        text = (ROOT / relative).read_text(encoding="utf-8")
        observed = re.findall(pattern, text)
        assert observed, f"{relative}: pattern not found ({name})"
        expected = deadlines.deadline(name).value
        for literal in observed:
            assert float(literal) == expected, (relative, name, literal)
        assert name not in {name for _, name in deadlines.CONSUMER_BINDINGS}


# --------------------------------------------------------------------------- #
# wp5-cleanup-levers: one owner per number on the snapshot-attempt family
# --------------------------------------------------------------------------- #


def test_cleanup_snapshot_attempt_record_has_no_consumerless_binding() -> None:
    """The merge drift at ``route.py:85`` is closed, explicitly and in one place.

    Merge ``a13-resolve`` bound ``mycelium_live.route._CLEANUP_SNAPSHOT_ATTEMPT_SECONDS``
    to this registry and then consumed it **nowhere**, while the route's live
    fallback slice is the measured 1.1 s ``_CLEANUP_SNAPSHOT_SLICE_SECONDS`` - so
    the code and the registry disagreed about what one snapshot attempt is, and a
    reader of ``route.py`` saw one attempt as 0.5 s against a route that hands out
    1.1 s.

    The interval the route actually applies is ``cleanup_fallback_reserve_seconds``
    (subtracted at the fanout boundary); the duration it hands to a resumed
    attempt is ``cleanup_snapshot_slice_seconds``.  The attempt record is the
    composition term that keeps the reserve honest, so it stays registered, gains
    an explicit statement of how it differs from the slice, and carries **no**
    module-level consumer symbol instead of a second unread copy.
    """

    assert "cleanup_snapshot_attempt_seconds" in deadlines.NON_BINDING_DEADLINES
    assert (
        "mycelium_live.route._CLEANUP_SNAPSHOT_ATTEMPT_SECONDS"
        not in dict(deadlines.CONSUMER_BINDINGS)
    )
    route = importlib.import_module("mycelium_live.route")
    assert not hasattr(route, "_CLEANUP_SNAPSHOT_ATTEMPT_SECONDS")
    assert "_CLEANUP_SNAPSHOT_ATTEMPT_SECONDS" not in BOUND_SOURCES[
        "mycelium_live/route.py"
    ]

    attempt = deadlines.deadline("cleanup_snapshot_attempt_seconds")
    slice_record = deadlines.deadline("cleanup_snapshot_slice_seconds")
    # Same number as the reserve by construction, and strictly different from -
    # smaller than - the duration one resumed attempt is actually handed.
    assert attempt.value == deadlines.CLEANUP_FALLBACK_RESERVE_SECONDS == 3.5
    assert slice_record.value == deadlines.CLEANUP_SNAPSHOT_SLICE_SECONDS == 4.0
    assert slice_record.value > attempt.value
    # The records must say so explicitly, because the two values must differ.
    for token in (
        "cleanup_fallback_reserve_seconds",
        "cleanup_snapshot_slice_seconds",
        "must not be collapsed",
    ):
        assert token in attempt.derivation, (token, attempt.derivation)
        assert token in slice_record.derivation, (token, slice_record.derivation)
    # The applied interval still has exactly one owner: the reserve the route
    # subtracts, which this record sizes.
    assert (
        "mycelium_live.route._CLEANUP_FALLBACK_RESERVE_SECONDS"
        in dict(deadlines.CONSUMER_BINDINGS)
    )


# --------------------------------------------------------------------------- #
# The existing suites' seam: names stay importable and monkeypatchable.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dotted", MONKEYPATCHED_DOTTED_PATHS)
def test_deadline_names_stay_monkeypatchable_by_dotted_path(
    monkeypatch: pytest.MonkeyPatch, dotted: str
) -> None:
    registry_name = dict(deadlines.CONSUMER_BINDINGS)[dotted]
    original = deadlines.deadline(registry_name).value
    assert _resolve(dotted) == original

    monkeypatch.setattr(dotted, 0.02)
    assert _resolve(dotted) == 0.02

    # Patching the consumer module must never mutate the registry itself.
    assert deadlines.deadline(registry_name).value != 0.02


def test_route_cleanup_names_are_importable_module_globals() -> None:
    route = importlib.import_module("mycelium_live.route")
    for attribute, registry_name in (
        ("_IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS", "idle_keepalive_probe_deadline_seconds"),
        ("_CLEANUP_RECEIPT_POLL_SECONDS", "cleanup_receipt_poll_seconds"),
        ("_CLEANUP_FALLBACK_RESERVE_SECONDS", "cleanup_fallback_reserve_seconds"),
        ("_CLEANUP_INLINE_RECEIPT_GRACE_SECONDS", "cleanup_inline_receipt_grace_seconds"),
        (
            "_CLEANUP_RECONCILIATION_HORIZON_SECONDS",
            "cleanup_reconciliation_horizon_seconds",
        ),
        ("_CLEANUP_RECONCILIATION_WORKERS", "cleanup_reconciliation_workers"),
    ):
        assert getattr(route, attribute) == deadlines.deadline(registry_name).value


# --------------------------------------------------------------------------- #
# wp5-d9: the advertised cleanup bound is read from the registry (round-50 closed)
# --------------------------------------------------------------------------- #

ADVERTISED_CLEANUP_PAYLOAD_KEY = "maximum_total_cleanup_ms"


def _node_interruptibility_projection() -> dict[str, Any]:
    """Build the physical node's real ``interruptibility`` projection offline.

    ``PhysicalNodeService._snapshot`` is the exact code path that produces the
    advertised ``maximum_total_cleanup_ms`` payload key - the value the route's
    status projection and the UI read - so the wp5-d9 anti-drift assertions are
    made against executed node code, not against its source text.  The service
    object is built with ``object.__new__``: no host, model, sidecar or
    subprocess is touched, and the signing call is replaced with a passthrough
    that keeps the details verbatim.
    """

    import physical_inference_node

    class _FakeRuntime:
        def kv_snapshot(self) -> dict[str, Any]:
            return {
                "mode": "stage_local_kv",
                "backend": "numpy",
                "states": {},
                "active_state_count": 0,
                "active_kv_bytes": 0,
                "maximum_observed_work_unit_ms": 125.0,
                "observed_work_unit_count": 24,
            }

    with tempfile.TemporaryDirectory() as scratch:
        service = object.__new__(physical_inference_node.PhysicalNodeService)
        service.state = "CONFIGURED"
        service.runtime = _FakeRuntime()
        service.capacity = None
        service.transport = None
        service.sidecar = None
        service.runtime_identity = {
            "backend": "numpy",
            "dtype": "float32",
            "quantization": "none",
        }
        service.artifact_root = Path(scratch)
        service._last_cancellation = None
        service._signed_result = lambda event, details=None: {
            "event": event,
            "details": details,
        }
        result = service._snapshot({})
    interruptibility = result["details"]["interruptibility"]
    assert ADVERTISED_CLEANUP_PAYLOAD_KEY in interruptibility, interruptibility
    return interruptibility


def test_advertised_cleanup_bound_fits_inside_the_route_fanout_window() -> None:
    """wp5-d9 (PLAN D9): the round-50 advertised-bound defect is closed.

    A peer may never advertise more cleanup time than the route has to fan
    cancellation out: ``advertised <= route_cleanup_fanout_window_ms``, and the
    measured cleanup cost (``infer_cancel_wait`` 0.000 s owner / 0.001 s laptop,
    ``runs/a13-macos-demo-export/round64-c5-met-per-placement-work-on-both-macs.md``)
    is three orders of magnitude below the bound.
    """

    advertised = deadlines.advertised_maximum_total_cleanup_ms
    window = deadlines.route_cleanup_fanout_window_ms
    assert advertised <= window, f"advertised {advertised} ms > window {window} ms"
    # The D9 composition, re-derived 2026-09-20 from the MEASURED two-Mac
    # cleanup (1.83 s / 3.18 s): 8.0 s budget - 3.5 s fallback reserve = 4.5 s
    # window, and the advertised bound is the admitted maximum less one slack:
    # 4_500 - 200 - 100 = 4_200 ms.
    assert advertised == 4_200
    assert window == 4_500
    assert (
        deadlines.check_peer_advertised_cleanup_bound(
            advertised_cleanup_ms=advertised,
            fanout_window_ms=window,
        )
        is None
    )
    assert deadlines.check_composition() == []


def test_shipped_advertised_bound_is_admitted_by_the_routes_own_predicate() -> None:
    """Merge a13-resolve: the shipped node must not refuse its own advertisement.

    The a13 lane's route-side predicate
    (``mycelium_live.route.admit_peer_cleanup_bound``) refuses any advertised
    bound above ``window - transport margin``; the registry derives the shipped
    bound as ``window - margin - slack``, so the shipped advertisement is
    strictly admissible (1_200 ms <= 1_300 ms admitted).  Both sides of the
    comparison read the same registry records, so the predicate and the
    advertisement cannot drift apart - and if either number moves alone, this
    test fails instead of the node refusing itself at startup.
    """

    route = importlib.import_module("mycelium_live.route")
    advertised = deadlines.advertised_maximum_total_cleanup_ms
    assert route.admit_peer_cleanup_bound(advertised, node_id="self") == advertised
    assert advertised <= (
        deadlines.route_cleanup_fanout_window_ms
        - deadlines.PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS
    )
    # And the route's own bound constants are the registry's records, so the
    # predicate cannot be reading a second copy of the margin or the reserve.
    assert route.PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS == (
        deadlines.PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS
    )
    assert route._CLEANUP_FALLBACK_RESERVE_SECONDS == (
        deadlines.CLEANUP_FALLBACK_RESERVE_SECONDS
    )


def test_node_payload_reads_the_registry_symbol_and_follows_a_registry_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The node holds no second copy: the registry symbol moves the payload."""

    projection = _node_interruptibility_projection()
    assert projection[ADVERTISED_CLEANUP_PAYLOAD_KEY] == (
        deadlines.advertised_maximum_total_cleanup_ms
    )

    monkeypatch.setattr(deadlines, "advertised_maximum_total_cleanup_ms", 1_234)
    mutated = _node_interruptibility_projection()
    assert mutated[ADVERTISED_CLEANUP_PAYLOAD_KEY] == 1_234


def test_advertised_bound_is_derived_from_the_window_and_pinned_in_the_canonical_artifacts() -> None:
    """The advertised bound is derived from the window, and the produced
    artifacts (canonical fixture + canonical producer) carry that value."""

    assert deadlines.advertised_maximum_total_cleanup_ms == (
        deadlines.derive_advertised_maximum_total_cleanup_ms()
    )
    assert (
        deadlines.derive_advertised_maximum_total_cleanup_ms(fanout_window_ms=1_750)
        == 1_450
    )
    assert (
        deadlines.derive_advertised_maximum_total_cleanup_ms(
            budget_seconds=3.0,
            fallback_reserve_seconds=0.5,
        )
        == 2_200
    )
    # The window itself is the largest value only when nothing is held back for
    # transport: the three terms stay separable.
    assert (
        deadlines.derive_advertised_maximum_total_cleanup_ms(
            fanout_window_ms=1_500,
            transport_margin_ms=0,
            slack_ms=0,
        )
        == 1_500
    )

    fixture = json.loads(
        (
            ROOT / "contracts" / "compatibility-fixtures" / "live-route-status-v1.json"
        ).read_text(encoding="utf-8")
    )
    observed: list[Any] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == ADVERTISED_CLEANUP_PAYLOAD_KEY:
                    observed.append(item)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(fixture)
    assert observed, "the canonical live route status fixture lost the advertised bound"
    assert set(observed) == {deadlines.advertised_maximum_total_cleanup_ms}, observed

    producer = (ROOT / "scripts" / "generate_contract_fixtures.py").read_text(
        encoding="utf-8"
    )
    pinned = re.findall(
        r'"maximum_total_cleanup_ms":\s*([A-Za-z_][A-Za-z0-9_.]*)', producer
    )
    assert pinned == ["advertised_maximum_total_cleanup_ms"], pinned


# --------------------------------------------------------------------------- #
# Class invariant: the closed member-runtime bundle must stay importable.
# --------------------------------------------------------------------------- #


def _runtime_closure_repo_dependencies(relative: str) -> set[str]:
    """Repo-root modules a closure module imports at top level."""

    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
    return {
        name
        for name in names
        if (ROOT / f"{name}.py").is_file() or (ROOT / name / "__init__.py").is_file()
    }


def test_member_runtime_closure_has_no_import_outside_its_file_set() -> None:
    """The least-authority member bundle must stay a closed, importable set.

    ``mycelium_member_runtime.RUNTIME_PATHS`` is copied to a private directory
    and import-probed from there.  A repo-root import that is not in that file
    set makes the probe fail (``member_runtime_import_probe_failed``) - the
    defect this test catches for the class.  Binding a deadline inside a closure
    module therefore requires adding ``mycelium_deadlines.py`` to the closure in
    the same change; see ``PENDING_MEMBER_RUNTIME_CLOSURE_BINDINGS``.
    """

    runtime = importlib.import_module("mycelium_member_runtime")
    closure = tuple(runtime.RUNTIME_PATHS)
    closure_modules = {
        Path(relative).with_suffix("").as_posix().replace("/", ".") for relative in closure
    }
    closure_modules |= {
        Path(relative).parent.as_posix().replace("/", ".")
        for relative in closure
        if Path(relative).name == "__init__.py"
    }

    outside: set[str] = set()
    for relative in closure:
        for dependency in _runtime_closure_repo_dependencies(relative):
            if dependency in closure_modules:
                continue
            # Skip purely namespace parents that only exist as packages of
            # modules already inside the closure.
            if any(module.startswith(f"{dependency}.") for module in closure_modules):
                continue
            outside.add(f"{relative} -> {dependency}")
    assert outside == set(), sorted(outside)
