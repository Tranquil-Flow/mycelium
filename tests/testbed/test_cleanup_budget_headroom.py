# SPDX-License-Identifier: AGPL-3.0-only
"""The cleanup budget must cover the MEASURED two-Mac cleanup latency, not a derived window.

Measured on the two-Mac route and recorded at ``mycelium_deadlines.py:306-314``: the relayed peer's
end-to-end cleanup returned at **1.83 s / 3.18 s** against the then 1.5 s window -- it did not fit. The
peer nevertheless advertised the window-derived 1,200 ms, so the route's admission predicate could never
see the real latency, qualification admitted the peer, and the failure surfaced mid-request:

    p75r1 and p77r1: command_cleanup_receipt_missing -> failed/request_cleanup_unproven
    (router_port.py:816-855; the waiter route.py:2745-2870 grants 1.5 s of inline grace and then
     one 1.1 s slice, against a peer needing 1.83-3.18 s)

Owner decision, 2026-09-20: re-derive the budget FROM that measurement, the same pattern as b7f0e07 for
the RSS bound. These tests pin the derivation so it cannot silently drift back to a window-shaped number.
"""

from mycelium_deadlines import (
    CANCEL_AND_CLEANUP_BUDGET_SECONDS,
    CLEANUP_INLINE_RECEIPT_GRACE_SECONDS,
    CLEANUP_SNAPSHOT_SLICE_SECONDS,
    advertised_maximum_total_cleanup_ms,
    route_cleanup_fanout_window_ms,
)

#: The a13 lane's measurement of the two-Mac peer's end-to-end cleanup.
MEASURED_CLEANUP_TYPICAL_MS = 1_830
MEASURED_CLEANUP_WORST_MS = 3_180


def test_one_recovery_slice_covers_the_measured_worst_case_with_headroom():
    """A slice shorter than one real cleanup is not a recovery path, it is a second way to miss."""

    assert CLEANUP_SNAPSHOT_SLICE_SECONDS >= (MEASURED_CLEANUP_WORST_MS / 1000.0) * 1.15


def test_the_advertised_bound_clears_the_measured_worst_case():
    """The advertisement must describe the peer, so it must clear the peer's measured latency."""

    assert advertised_maximum_total_cleanup_ms >= int(MEASURED_CLEANUP_WORST_MS * 1.10)


def test_the_advertisement_still_sits_inside_the_window():
    """The predicate keeps its teeth: the bound stays below the window it must fit in."""

    assert advertised_maximum_total_cleanup_ms < route_cleanup_fanout_window_ms


def test_the_window_clears_the_measured_worst_case():
    """The route must actually have the interval the peer needs, not merely advertise one."""

    assert route_cleanup_fanout_window_ms > MEASURED_CLEANUP_WORST_MS


def test_the_grace_and_one_slice_fit_inside_the_budget():
    """Every other record is a partition of the budget, never an extension of it."""

    assert (
        CLEANUP_INLINE_RECEIPT_GRACE_SECONDS + CLEANUP_SNAPSHOT_SLICE_SECONDS
        <= CANCEL_AND_CLEANUP_BUDGET_SECONDS
    )


def test_the_budget_was_not_quietly_shrunk_back_to_the_window_derived_value():
    """Guard against a silent revert to the 2 s total that could not fit the measured peer."""

    assert CANCEL_AND_CLEANUP_BUDGET_SECONDS >= 8.0
