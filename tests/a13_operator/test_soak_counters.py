"""Criterion 5 is measured by ABSENCE, so its counters must not be satisfiable by accident.

Zero stuck, zero duplicates, zero control-loop deaths -- each of those is a claim that something did NOT
happen, and a counter that can pass while the thing happened is worse than no counter.
"""
import importlib.util
import pathlib

_p = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator/wp8_05_soak.py'
_spec = importlib.util.spec_from_file_location('soakmod', _p)
soak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soak)


def _rec(ok=True, rid='r', total=1.0, cancel=False, confirmed=False, terminal='completed'):
    return {'ok': ok, 'request_id': rid, 'total_s': total, 'cancelled_requested': cancel,
            'cancelled_confirmed': confirmed, 'terminal': terminal}


def test_a_clean_soak_counts_nothing():
    s = soak.summarize([_rec(rid='a'), _rec(rid='b')], expected=2)
    assert (s['completed'], s['stuck'], s['duplicates'], s['met_floor']) == (2, 0, 0, True)


def test_a_failed_request_is_stuck_not_absent():
    """Failure is evidence, not nothing: a request that never terminal'd must show as stuck."""
    s = soak.summarize([_rec(rid='a'), _rec(ok=False, rid='b')], expected=2)
    assert s['stuck'] == 1 and s['stuck_request_ids'] == ['b']


def test_a_cancellation_intent_alone_does_not_count_as_stuck():
    """Requested cancellations are counted separately -- they are an experiment, not a failure."""
    s = soak.summarize([_rec(rid='a'), _rec(ok=False, rid='b', cancel=True)], expected=2)
    assert s['stuck'] == 0 and s['cancelled_requested'] == 1 and s['cancelled_confirmed'] == 0


def test_duplicates_are_detected_by_identity_not_by_count():
    """Two completions sharing a request_id is the failure; counting rows would hide it."""
    s = soak.summarize([_rec(rid='same'), _rec(rid='same')], expected=2)
    assert s['duplicates'] == 1 and s['duplicate_request_ids'] == ['same']


def test_missing_the_floor_is_recorded_not_inferred():
    s = soak.summarize([_rec(rid='a')], expected=110)
    assert s['met_floor'] is False and s['requests'] == 1


def test_percentiles_survive_a_single_sample():
    s = soak.summarize([_rec(rid='a', total=5.0)], expected=1)
    assert s['p50_total_s'] == 5.0 and s['p95_total_s'] == 5.0


def test_a_cancellation_is_only_confirmed_when_the_product_says_cancelled():
    s = soak.summarize([_rec(rid='a', ok=False, cancel=True, confirmed=True, terminal='cancelled')],
                       expected=1)
    assert s['cancelled_confirmed'] == 1
