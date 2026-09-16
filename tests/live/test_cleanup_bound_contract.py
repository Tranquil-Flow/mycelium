"""The node's declared cleanup bound must fit inside the owner's real wait.

Measured failure this pins (rounds 47-50; see
`outputs/a13-macos-evidence/round50-per-placement-work-and-command-deadline.md`):
the node advertised ``maximum_total_cleanup_ms = 2000`` while the owner's
cancellation budget is an immutable 2000 ms with its fallback-snapshot reserve
taken out of it. The owner therefore waited at most ~1500 ms (measured fanout TTL
1.499 s). A cleanup landing in the top quarter of the declared range could only
ever miss that deadline, so ``pending.cleanup_receipt`` stayed ``None``,
``_record_command_terminal`` raised ``command_cleanup_receipt_missing``, and the
request failed with the SSE terminal unpublished by design.

These assertions read BOTH real constants, so changing either side alone fails the
test. The invariant is: a node must never advertise a cleanup bound its own owner
cannot honour.
"""
import pytest

import mycelium_deadlines
from mycelium_live.route import (
    PhysicalLiveRoute,
    _CLEANUP_FALLBACK_RESERVE_SECONDS,
    _CLEANUP_INLINE_RECEIPT_GRACE_SECONDS,
    _CLEANUP_SNAPSHOT_SLICE_SECONDS,
    admit_peer_cleanup_bound,
)
from physical_inference_node import MAXIMUM_TOTAL_CLEANUP_MS

# The owner-side deadline is structurally immutable: `PhysicalLiveRoute.cancel_request`
# raises `invalid_cancellation_deadline` for any deadline outside
# (cancellation_started_at, cancellation_started_at + 2.0].
IMMUTABLE_OWNER_CLEANUP_BUDGET_MS = 2_000

# Command delivery plus receipt return must fit inside the owner's window too, so the
# declared bound cannot sit flush against it.
MINIMUM_TRANSPORT_MARGIN_MS = 200


def _owner_fanout_window_ms() -> int:
    return IMMUTABLE_OWNER_CLEANUP_BUDGET_MS - int(
        _CLEANUP_FALLBACK_RESERVE_SECONDS * 1_000
    )


def test_declared_cleanup_bound_fits_the_owner_fanout_window() -> None:
    assert MAXIMUM_TOTAL_CLEANUP_MS <= _owner_fanout_window_ms()


def test_declared_bound_leaves_transport_margin_below_the_window() -> None:
    margin = _owner_fanout_window_ms() - MAXIMUM_TOTAL_CLEANUP_MS
    assert margin >= MINIMUM_TRANSPORT_MARGIN_MS


def test_fallback_reserve_still_covers_a_whole_snapshot_attempt() -> None:
    """The reserve is the receipt-recovery path; widening the window by starving it
    would trade a measurable failure for an unmeasurable one.

    wp5-cleanup-levers: the attempt interval is the registry's own record, not a
    second route-local copy - ``_CLEANUP_SNAPSHOT_ATTEMPT_SECONDS`` was bound in
    ``mycelium_live.route`` with no consumer while the route's live slice is the
    measured 1.1 s, so this guard now reads the record directly and the record
    is explicitly non-binding (``NON_BINDING_DEADLINES``) rather than duplicated.
    """
    assert (
        _CLEANUP_FALLBACK_RESERVE_SECONDS
        >= mycelium_deadlines.CLEANUP_SNAPSHOT_ATTEMPT_SECONDS
    )
    assert 0 < _CLEANUP_FALLBACK_RESERVE_SECONDS < IMMUTABLE_OWNER_CLEANUP_BUDGET_MS / 1_000
    # The two numbers must differ (the held-back interval vs the duration handed
    # out), and the registry records say so explicitly.
    attempt = mycelium_deadlines.deadline("cleanup_snapshot_attempt_seconds")
    assert "cleanup_snapshot_slice_seconds" in attempt.derivation
    assert "cleanup_fallback_reserve_seconds" in attempt.derivation
    assert _CLEANUP_SNAPSHOT_SLICE_SECONDS > attempt.value


def test_declared_bound_excludes_the_old_unhonourable_literal() -> None:
    """Regression guard: 2000 ms was the whole owner budget and can never be honoured."""
    assert MAXIMUM_TOTAL_CLEANUP_MS < IMMUTABLE_OWNER_CLEANUP_BUDGET_MS


def test_fallback_attempt_slice_covers_a_measured_peer_round_trip() -> None:
    """A slice shorter than a real round trip is a second way to miss.

    Measured successful request-scoped snapshot round trips to the relayed
    laptop were 0.875-1.066 s, and the first fallback attempt's 0.5 s slice was
    never claimed before it expired
    (`outputs/a13-macos-evidence/round52-cleanup-provable-within-window.md` §2).
    """

    assert _CLEANUP_SNAPSHOT_SLICE_SECONDS >= 1.0


def test_fallback_attempt_slice_fits_the_owner_window_after_the_inline_grace() -> None:
    """The recovery path must fit inside the immutable budget it recovers into.

    This is the owner-side mirror of decision 2: just as a peer may not
    advertise a cleanup bound its owner cannot honour, the owner's own fallback
    proof attempt may not overrun the remaining window it is spent from.
    """

    assert (
        _CLEANUP_INLINE_RECEIPT_GRACE_SECONDS + _CLEANUP_SNAPSHOT_SLICE_SECONDS
        <= IMMUTABLE_OWNER_CLEANUP_BUDGET_MS / 1_000
    )


def test_advertised_cleanup_bound_is_admitted_inside_the_window_with_margin() -> None:
    """Decision 2: a peer bound that fits the route window (with margin) is admitted."""

    assert admit_peer_cleanup_bound(1_200, node_id="node-a") == 1_200
    assert admit_peer_cleanup_bound(1_300, node_id="node-a") == 1_300


def test_preview50_advertised_bound_is_refused_by_name() -> None:
    """RED on the preview.50 value: 2000 ms was the whole owner budget.

    This is decision 4(a)'s required unit test: the advertised 2000 ms against a
    1500 ms window, and here against its 200 - margin admitted ceiling.
    """

    with pytest.raises(ValueError) as raised:
        admit_peer_cleanup_bound(2_000, node_id="node-a")

    message = str(raised.value)
    assert message.startswith("peer_cleanup_bound_exceeds_route_window:node-a:")
    assert "advertised=2000" in message
    assert "window=1500" in message


def test_measured_peer_cleanup_latency_is_refused() -> None:
    """The round-52 §13 measurement does not fit, and is now refused at the boundary.

    Measured on preview.52: the relayed peer's `infer_cancel_wait` returned
    complete 3.180 s after issue (and its request-scoped snapshot at 2.418 s)
    against a 1500 ms fanout window. A bound derived from that measurement must
    not be admitted, which is decision 3's "if it does not fit, the fix is the
    delivery path, not the budget" expressed as a startup refusal.
    """

    for measured_ms in (1_418, 1_830, 2_418, 3_180):
        with pytest.raises(ValueError) as raised:
            admit_peer_cleanup_bound(measured_ms, node_id="remote-mac-e9fe")
        assert "peer_cleanup_bound_exceeds_route_window" in str(raised.value)

    # 1418 ms is the only one that would have fitted the raw window (1500 ms)
    # but it still breaches the admitted ceiling (1300 ms), which is the margin
    # the transport needs for delivery plus receipt return.
    assert admit_peer_cleanup_bound(1_200, node_id="remote-mac-e9fe") == 1_200


def test_absent_advertisement_is_not_silently_admitted() -> None:
    """A peer that publishes no bound returns None; the caller must decide.

    The predicate never invents a bound, and never green-lights absence.
    """

    assert admit_peer_cleanup_bound(None, node_id="node-a") is None
    assert admit_peer_cleanup_bound("1200", node_id="node-a") is None
    assert admit_peer_cleanup_bound(True, node_id="node-a") is None


def _qualification_route(advertised_ms: int) -> PhysicalLiveRoute:
    """A route holding one peer's freshest signed snapshot observation.

    Only the attributes ``live_attestation`` reads before its observation-window
    check are populated; everything else is deliberately absent.
    """

    route = object.__new__(PhysicalLiveRoute)
    route._request_outputs = {"request-a": (31,)}
    route.is_alive = lambda: True  # type: ignore[method-assign]
    route._sessions = {"remote-mac-e9fe": object()}
    route._snapshot_all = lambda: None  # type: ignore[method-assign]
    route._signed_observations = [
        {
            "observation": {
                "node_id": "remote-mac-e9fe",
                "event": "snapshot",
                "details": {
                    "interruptibility": {"maximum_total_cleanup_ms": advertised_ms}
                },
            }
        }
    ]
    return route


def test_the_route_refuses_an_over_reaching_peer_at_startup_qualification() -> None:
    """wp5-cleanup-levers: the admission predicate is on the qualification path.

    The arithmetic packet found ``admit_peer_cleanup_bound`` landed but wired
    **nowhere** (no non-test call site), so the named refusal the whole
    composition assumes was inert: a peer that advertises more cleanup than the
    route can wait for was admitted at startup and failed mid-request instead.
    The route's qualification-only evidence path
    (``PhysicalLiveRoute.live_attestation``, driven by
    ``mycelium_live.supervisor._qualify_open_route``) is the consumer that reads
    each peer's advertised bound, so the refusal lives there - never mid-request.
    """

    for measured_ms in (1_830, 2_418, 3_180):
        with pytest.raises(RuntimeError) as raised:
            _qualification_route(measured_ms).live_attestation(request_id="request-a")
        message = str(raised.value)
        assert message.startswith(
            "peer_cleanup_bound_exceeds_route_window:remote-mac-e9fe:"
        ), message
        assert f"advertised={measured_ms}" in message
        assert "admitted=1300" in message
        assert "window=1500" in message

    # The shipped advertisement passes the same predicate on the same path, so
    # the refusal discriminates the bound rather than the qualification wiring.
    with pytest.raises(RuntimeError) as shipped:
        _qualification_route(MAXIMUM_TOTAL_CLEANUP_MS).live_attestation(
            request_id="request-a"
        )
    assert "peer_cleanup_bound_exceeds_route_window" not in str(shipped.value)
    assert "live_attestation_observation_window_incomplete" in str(shipped.value)
