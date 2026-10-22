# SPDX-License-Identifier: AGPL-3.0-only
"""Recovery assertions must observe native completion, not count HTTP polls."""
from threading import Event

import pytest

import test_concurrent_pump_expired_outbound_parent as expired
import test_concurrent_pump_outbound_failure_recovery as rejected


@pytest.mark.parametrize('scenario', ['rejected', 'expired'])
def test_recovery_observer_waits_for_native_completion(tmp_path, monkeypatch, scenario):
    """Hold a real native result until the observer synchronizes with its future.

    The old twelve-poll observer deterministically fails while this result is
    held. A scheduler may legally leave the executor unobserved for that whole
    loop: step() is explicitly nonblocking. Release on observation, never on a
    sleep or elapsed-time guess, and release in finally even on the RED path.
    """
    module, exercise = {
        'rejected': (rejected, rejected.test_a_rejected_poll_request_is_dropped_and_resigned),
        'expired': (expired, expired.test_concurrent_pump_survives_an_expired_envelope),
    }[scenario]
    original_pump = module._pump
    release = Event()
    observed = Event()
    submitted = []

    def make_pump(*args, **kwargs):
        pump, worker = original_pump(*args, **kwargs)
        submit = pump._executor.submit
        close = pump.close

        def submit_held(function, *args, **kwargs):
            def held():
                native = function(*args, **kwargs)
                release.wait()  # Always released by observation or close/finally.
                return native

            future = submit(held)
            result = future.result

            def observe(timeout=None):
                observed.set()
                release.set()
                return result(timeout=timeout)

            monkeypatch.setattr(future, 'result', observe)
            submitted.append((future, result))
            return future

        def close_released():
            release.set()
            # Join before closing the real native worker, including on RED.
            try:
                for _, result in submitted:
                    result(timeout=2)
            finally:
                close()

        monkeypatch.setattr(pump._executor, 'submit', submit_held)
        monkeypatch.setattr(pump, 'close', close_released)
        return pump, worker

    monkeypatch.setattr(module, '_pump', make_pump)
    try:
        exercise(tmp_path, monkeypatch)
        assert len(submitted) == 1
        assert observed.is_set(), 'recovery observer never synchronized with native completion'
    finally:
        release.set()
