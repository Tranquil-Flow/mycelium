# SPDX-License-Identifier: AGPL-3.0-only
"""An expired unconfirmed intent is PROVABLY DEAD and must not wedge the owner forever.

The measured wedge: packaged_app_daemon.py:21 SIGKILLs the app after a 15 s grace (exit_code -9), an
in-flight enqueue is never confirmed, and `managed_control_reconciliation_required` then raises for every
later command -- registry health `unavailable`, route_ready False, gateway 503. Two escape hatches existed
(reconcile a still-fresh request; retire a different-run intent) and neither could cover an EXPIRED intent
bound to the CURRENT run.

These tests pin the narrowness, which is the whole point: a fresh current-run intent must STILL fail closed,
and anything uncertain must be treated as fresh.
"""
import pathlib

# import as a PACKAGE module: control_session uses relative imports (.authority, .durable_requests),
# so loading it by path raises "attempted relative import with no known parent package".
from mycelium_a13_macos import control_session as cs


def _row(expires_at, run_id='r1', deployment_id='d1'):
    return {
        'enqueue_confirmed': False,
        'binding': {'run_id': run_id, 'deployment_id': deployment_id},
        'enqueue_request': {'operation': 'enqueue',
                            'envelope': {'statement': {'expires_at': expires_at}}},
    }


def test_an_expired_intent_is_provably_dead():
    assert cs._intent_expired(_row(100.0), 200.0) is True


def test_a_fresh_intent_is_not():
    """The fail-closed case the guard exists for: a live enqueue must keep blocking."""
    assert cs._intent_expired(_row(300.0), 200.0) is False


def test_exactly_at_expiry_counts_as_expired():
    assert cs._intent_expired(_row(200.0), 200.0) is True


def test_a_missing_expires_at_is_treated_as_fresh():
    """Predicting nothing rather than guessing: an unreadable intent must still fail closed."""
    row = _row(1.0)
    del row['enqueue_request']['envelope']['statement']['expires_at']
    assert cs._intent_expired(row, 9e9) is False


def test_a_missing_prepared_request_is_treated_as_fresh():
    row = _row(1.0)
    del row['enqueue_request']
    assert cs._intent_expired(row, 9e9) is False


def test_a_malformed_clock_is_treated_as_fresh():
    assert cs._intent_expired(_row(1.0), 'not-a-clock') is False


def test_the_predicate_is_actually_wired_into_the_retire_set():
    """A helper nothing calls would be worse than no helper: it would look fixed."""
    src = (pathlib.Path(__file__).resolve().parents[2] /
           'mycelium_a13_macos/control_session.py').read_text()
    i = src.index('retired = [key for key in unresolved')
    assert '_intent_expired(' in src[i:i + 500]
