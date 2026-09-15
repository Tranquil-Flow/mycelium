"""wp2-1: the deadline/budget registry and its composition invariants.

Covers (PLAN 5.2 item 1):

* the **round-50 RED case**: the preview.50 composition (peer advertises
  2000 ms, snapshot attempt / fallback reserve 500 ms, total budget 2000 ms ->
  route fanout window 1500 ms) must be reported as exactly
  ``peer_advertised_cleanup_bound_exceeds_route_fanout_window``;
* an honest, **deliberately pinned** statement of today's shipped composition;
* registry hygiene (derivation, units, names);
* a **no-drift** guard: the registry values equal the historical literals that
  shipped in the consumer modules (written as literals below on purpose);
* the consumer constants stay importable **and monkeypatchable by dotted string
  path**, and the bound ones actually read the registry.
"""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path
from typing import Any

import pytest

import mycelium_deadlines as deadlines

ROOT = Path(__file__).resolve().parents[2]

#: The violation set shipped today. The round-50 defect (PLAN D9) is real and
#: unfixed: the node advertises a 2000 ms cleanup bound while the route's fanout
#: window is only 1500 ms (2000 ms budget - 500 ms fallback reserve).
#:
#: THIS IS A DELIBERATE PIN. When WP5/A13 fixes the shipping composition (D9:
#: validate the advertised bound against the fanout window and derive the
#: advertised value from measured delivery+execution+receipt latency) this test
#: MUST go RED. That red is the signal that the defect is gone - update this
#: pinned set deliberately in the same change, never by loosening the checker.
PINNED_SHIPPED_VIOLATED_CODES = frozenset(
    {deadlines.REASON_PEER_ADVERTISED_CLEANUP_BOUND_EXCEEDS_ROUTE_FANOUT_WINDOW}
)

#: Historical literals as they shipped, transcribed by hand from the consumer
#: modules (file:line cited in the registry's ``derivation``). Deliberately NOT
#: read from the registry: this is the independent side of the no-drift check.
HISTORICAL_LITERALS: dict[str, float | int] = {
    "cancel_and_cleanup_budget_seconds": 2.0,
    "cleanup_fallback_reserve_seconds": 0.5,
    "cleanup_snapshot_attempt_seconds": 0.5,
    "cleanup_inline_receipt_grace_seconds": 0.5,
    "cleanup_receipt_poll_seconds": 0.1,
    "cleanup_reconciliation_horizon_seconds": 60.0,
    "cleanup_reconciliation_workers": 4,
    "idle_keepalive_probe_deadline_seconds": 0.5,
    "peer_advertised_maximum_total_cleanup_ms": 2_000,
    "gateway_sse_disconnect_poll_seconds": 0.1,
    "gateway_producer_stall_poll_seconds": 0.05,
    "route_health_probe_deadline_seconds": 0.5,
    "command_interruption_and_cleanup_max_ms": 2_000,
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

#: Constants this packet bound to the registry, by file. Each must be assigned
#: from ``mycelium_deadlines.<NAME>`` (module level, so monkeypatching the name
#: on the module still replaces exactly what the call sites read).
BOUND_SOURCES: dict[str, tuple[str, ...]] = {
    "mycelium_iroh_sidecar/client.py": ("PEER_CONNECT_DEADLINE_SECONDS",),
    "mycelium_live/member_transport.py": (
        "_GRANT_TTL_MS",
        "_AVAILABILITY_RECONCILE_MIN_SECONDS",
        "_AVAILABILITY_RECONCILE_MAX_SECONDS",
    ),
    "mycelium_node/process.py": ("_LAUNCH_HANDSHAKE_SECONDS",),
}

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
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    assigned: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            assigned[node.targets[0].id] = ast.unparse(node.value)
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
    """Today's shipped values violate exactly the pinned set (D9, unfixed).

    This asserts the violation; it never fails *because* of the violation.
    """

    shipped = deadlines.check_composition()
    assert set(shipped) == PINNED_SHIPPED_VIOLATED_CODES
    assert shipped == [
        deadlines.REASON_PEER_ADVERTISED_CLEANUP_BOUND_EXCEEDS_ROUTE_FANOUT_WINDOW
    ]

    # Restate the shipped composition in numbers so the pin is self-evident.
    assert deadlines.advertised_maximum_total_cleanup_ms == 2_000
    assert deadlines.route_cleanup_fanout_window_ms == 1_500
    assert (
        deadlines.advertised_maximum_total_cleanup_ms
        > deadlines.route_cleanup_fanout_window_ms
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
    assert deadlines.route_cleanup_fanout_window_seconds == 1.5
    assert deadlines.route_cleanup_fanout_window_ms == 1_500
    assert deadlines.advertised_maximum_total_cleanup_ms == 2_000
    assert deadlines.derive_route_cleanup_fanout_window_seconds() == (
        deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS
        - deadlines.CLEANUP_FALLBACK_RESERVE_SECONDS
    )
    with pytest.raises(ValueError):
        deadlines.derive_route_cleanup_fanout_window_ms(
            budget_seconds=2.0,
            fallback_reserve_seconds=0.3333,
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
    assert 0 < value < deadlines.route_cleanup_fanout_window_seconds == 1.5
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


def test_advertised_bound_is_the_registry_value_and_matches_the_canonical_producer() -> None:
    """PLAN D9's validated side: the node advertises the registry value.

    ``physical_inference_node.py`` and ``scripts/generate_contract_fixtures.py``
    are hash-pinned by ``contracts/contract-manifest.v1.json``, so this packet
    registers the advertised bound but does not rewrite those two files to read
    the registry symbol. This test is the drift guard in the meantime: it fails
    if the node's advertised number and the registry value ever disagree, and it
    keeps enforcing the registry symbol once the pin refresh lands and the
    payload is bound.
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

    # The canonical fixture producer pins the same number (never hand-edited).
    producer = (ROOT / "scripts" / "generate_contract_fixtures.py").read_text(
        encoding="utf-8"
    )
    pinned = re.findall(r'"maximum_total_cleanup_ms":\s*([0-9_]+)', producer)
    assert pinned == ["2_000"], pinned
    assert deadlines.advertised_maximum_total_cleanup_ms == 2_000


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
        ("_CLEANUP_SNAPSHOT_ATTEMPT_SECONDS", "cleanup_snapshot_attempt_seconds"),
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
