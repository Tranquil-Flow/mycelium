"""Criterion 5 is measured by ABSENCE, so its counters must not be satisfiable by accident.

Zero stuck, zero duplicates, zero control-loop deaths -- each of those is a claim that something did NOT
happen, and a counter that can pass while the thing happened is worse than no counter.
"""
import importlib.util
import json
import pathlib

_p = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator/wp8_05_soak.py'
_spec = importlib.util.spec_from_file_location('soakmod', _p)
soak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soak)


def _rec(ok=True, rid='r', total=1.0, cancel=False, confirmed=False,
         terminal: str | None = 'completed', terminal_code=None):
    """A per-request record shaped like the driver's: `succeeded` is the SUCCESSFUL terminal, which is
    not the same as `ok` (the driver sets ok when the stream produced tokens)."""
    return {'ok': ok, 'request_id': rid, 'total_s': total, 'cancelled_requested': cancel,
            'cancelled_confirmed': confirmed, 'terminal': terminal,
            'terminal_code': terminal_code, 'succeeded': terminal == 'completed'}


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


# --------------------------------------------------------------------------- #
# The whitewash this file's own docstring warns about: `ok` is not success.
# --------------------------------------------------------------------------- #

def test_a_cleanup_unproven_terminal_is_a_failure_not_a_completion():
    """p74r1's actual shape: tokens emitted, then the route says it could not prove cleanup.

    Before this counter existed the record was ok=True with terminal='failed', so it counted as
    COMPLETED -- the instrument that measures criterion 5 would have measured a broken request path as
    success. It must be counted as a failure and must make the criterion unmet.
    """
    records = [
        _rec(rid='good'),
        _rec(rid='blocked', terminal='failed', terminal_code='request_cleanup_unproven'),
    ]
    s = soak.summarize(records, expected=2)
    assert s['completed'] == 2, 'ok still counts the stream as having produced tokens'
    assert s['succeeded'] == 1, 'only one request reached a successful terminal'
    assert s['cleanup_unproven'] == 1
    assert s['cleanup_unproven_request_ids'] == ['blocked']
    assert s['failed_terminals'] == 1
    assert s['met_criterion_5'] is False
    assert s['criterion_5']['zero_cleanup_unproven'] is False


def test_a_request_with_tokens_and_no_terminal_is_unterminated_and_stuck():
    """A hang that emitted a token was invisible: ok=True, terminal=None, and not counted as stuck."""
    s = soak.summarize([_rec(rid='a'), _rec(rid='hung', terminal=None)], expected=2)
    assert s['unterminated'] == 1
    assert s['stuck_total'] == 1
    assert s['met_criterion_5'] is False
    assert s['failure_request_ids'] == ['hung']


def test_the_criterion_is_met_only_at_the_floor_with_confirmed_cancellations():
    records = [_rec(rid='r%d' % i) for i in range(100)]
    records += [_rec(rid='c%d' % i, ok=False, cancel=True, confirmed=True, terminal='cancelled')
                for i in range(10)]
    s = soak.summarize(records, expected=110, loop_deaths=0)
    assert s['succeeded'] == 100 and s['cancelled_confirmed'] == 10
    assert s['met_criterion_5'] is True


def test_cancellations_below_the_floor_keep_the_criterion_unmet():
    """Criterion 5 wants >=10 cancellations; the driver cannot wire them yet, so this must be False."""
    records = [_rec(rid='r%d' % i) for i in range(100)]
    records += [_rec(rid='c%d' % i, ok=False, cancel=True, confirmed=True, terminal='cancelled')
                for i in range(7)]
    s = soak.summarize(records, expected=100)
    assert s['criterion_5']['cancellations_met'] is False
    assert s['met_criterion_5'] is False


def test_a_loop_death_alone_unmeets_the_criterion():
    s = soak.summarize([_rec(rid='a')], expected=1, loop_deaths=1)
    assert s['criterion_5']['zero_loop_deaths'] is False and s['met_criterion_5'] is False


def test_the_criterion_names_what_this_driver_cannot_measure():
    """Honest limits, in the verdict itself: concurrency, a worker restart and a network change are NOT
    exercised by this driver, so a summary must not imply they were."""
    s = soak.summarize([_rec(rid='a')], expected=1)
    listed = ' | '.join(s['criterion_5']['unmeasured_by_this_driver'])
    assert 'concurrent' in listed and 'restart' in listed and 'network change' in listed
    assert s['criterion_5']['requests_met'] is False, 'one request is not >=100'


# --------------------------------------------------------------------------- #
# run_soak itself: the driver returns (samples, summary), and a tuple fed to
# dict.update() raises -- inside the block's own except, where it went unnoticed.
# --------------------------------------------------------------------------- #

def _fake_stream(records, calls):
    def fake(base, prompt, tokens, *, timeout, cancel_after_s=None, cancel_mode='disconnect'):
        calls.append({'cancel_after_s': cancel_after_s, 'cancel_mode': cancel_mode})
        terminal = 'cancelled' if cancel_after_s else 'completed'
        summary = {'ok': True, 'tokens': 3, 'total_s': 0.5, 'has_terminal': True,
                   'terminal': terminal, 'terminal_code': None, 'succeeded': terminal == 'completed',
                   'cancel_status': 202 if cancel_after_s else None}
        return [({'type': 'token'}, 0.5)], summary
    return fake


def test_run_soak_records_the_summary_not_the_drivers_tuple(tmp_path, monkeypatch):
    """REGRESSION: `record.update(result)` where result is (samples, summary) raises ValueError, and the
    block's own `except Exception` recorded it as a per-request failure -- so a soak measured nothing and
    reported every request as broken. The record must come from the SUMMARY."""
    calls = []
    monkeypatch.setattr(soak, '_stream', _fake_stream(None, calls))
    summary, path = soak.run_soak('http://127.0.0.1:1', requests=2, cancel_every=2,
                                  timeout=1, out_dir=str(tmp_path))
    assert summary['requests'] == 2
    assert summary['succeeded'] == 1, 'one plain request succeeded'
    assert summary['cancelled_requested'] == 1 and summary['cancelled_confirmed'] == 1
    assert summary['stuck_total'] == 0 and summary['failed_terminals'] == 0
    written = json.loads((tmp_path / ('soak-%s.json' % summary['label'])).read_text())
    assert all('error' not in r for r in written['records']), \
        'no record may carry the swallowed tuple error'
    assert all(r.get('ok') for r in written['records'])


def test_run_soak_asks_through_the_products_cancel_endpoint(tmp_path, monkeypatch):
    """The confirmed-cancel mode must be the one used for cancellation requests, and the plain mode for
    the rest -- a soak that queued the wrong mode would silently stop confirming anything."""
    calls = []
    monkeypatch.setattr(soak, '_stream', _fake_stream(None, calls))
    soak.run_soak('http://127.0.0.1:1', requests=2, cancel_every=2, timeout=1, out_dir=str(tmp_path))
    assert calls[0]['cancel_after_s'] is None and calls[0]['cancel_mode'] == 'disconnect'
    assert calls[1]['cancel_after_s'] == 2.0 and calls[1]['cancel_mode'] == soak.CANCEL_MODE


def test_a_cancellation_is_confirmed_only_by_the_products_terminal():
    """The endpoint's 202 is NOT a confirmation; only the product's own `cancelled` terminal is."""
    assert soak.cancellation_confirmed({'terminal': 'cancelled'}) is True
    assert soak.cancellation_confirmed({'terminal': 'completed', 'cancel_status': 202}) is False
    assert soak.cancellation_confirmed({'terminal': None, 'cancel_status': 202}) is False
    assert soak.cancellation_confirmed({'terminal': 'failed', 'cancel_status': 202}) is False


def test_a_cancelled_request_does_not_count_as_succeeded():
    """A confirmed cancellation is a settled request, not a successful one."""
    s = soak.summarize([_rec(rid='a'),
                        _rec(rid='c', ok=False, cancel=True, confirmed=True, terminal='cancelled')],
                       expected=2)
    assert s['cancelled_confirmed'] == 1
    assert s['succeeded'] == 1
    assert s['stuck_total'] == 0
