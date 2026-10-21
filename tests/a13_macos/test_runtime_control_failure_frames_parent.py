# SPDX-License-Identifier: AGPL-3.0-only
"""A control-loop death must say WHERE it escaped, not only which class.

Round p75r1 regression, measured on the target's own artifact:
``runtime-control-failure.private.json`` carried

    error_code: managed_control_message_expired
    exception_type: ManagedControlError
    state: blocked
    tokens: ["managed_control_message_expired"]

and NO frames at all.  So the record named the class and the code but not the site that raised it, and
the same wedge class has now recurred after being fixed twice at the pump level -- round 38's dead poll
envelope and round 54's ``reconciliation_required`` reply, both documented in
``mycelium_a13_macos/control_pump.py``.  Naming the site is the difference between a fix and a guess:
without it, the next occurrence is again indistinguishable from "the node simply stopped claiming work".

W4 closed this gap for the NATIVE diagnostics; this closes it for the runtime control loop, using the
same allowlist discipline (file basename, line, function -- never the message, never a path).
"""
import pytest

from mycelium_a13_macos.managed_runtime import _bounded_control_reason


def _raise_from_a_named_frame():
    raise RuntimeError('control_poll_reconciliation_required')


def _reason_for_a_raised_exception():
    try:
        _raise_from_a_named_frame()
    except RuntimeError as exc:
        return _bounded_control_reason(exc)
    raise AssertionError('unreachable: the helper above always raises')


def test_the_raising_frame_is_named():
    reason = _reason_for_a_raised_exception()
    assert reason['exception_type'] == 'RuntimeError'
    assert reason['frames'], 'the raising frame must be named, not only the class'
    assert any(frame['function'] == '_raise_from_a_named_frame' for frame in reason['frames'])


def test_frames_stay_bounded_and_leak_no_path():
    reason = _reason_for_a_raised_exception()
    blob = repr(reason)
    assert '://' not in blob and '/' not in blob and '?' not in blob
    assert len(reason['frames']) <= 24
    for frame in reason['frames']:
        assert frame['file'].endswith('.py')
        assert '/' not in frame['file']
        assert isinstance(frame['line'], int) and frame['line'] > 0
        assert isinstance(frame['function'], str) and 0 < len(frame['function']) <= 80


def test_a_class_only_exception_still_reports_its_class_with_no_frames():
    """An exception with no traceback must not invent frames -- and must not crash the recorder."""
    reason = _bounded_control_reason(RuntimeError('raised with no traceback attached'))
    assert reason['exception_type'] == 'RuntimeError'
    assert reason['frames'] == []
