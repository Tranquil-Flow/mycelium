# SPDX-License-Identifier: AGPL-3.0-only
"""A control-loop death must say WHY, not collapse to a generic code.

Round39 regression: the laptop's control loop died and the only thing reported
anywhere was ``runtime_control_failed`` -- the generic fallback.  That is
indistinguishable from a node that simply stopped claiming work, so the node's
real failure was invisible: every later owner command expired unanswered and the
request gateway lost all observation of the peer.

``_bounded_control_reason`` keeps the exception class plus a few allowlisted
tokens so the next occurrence is self-describing, while keeping the allowlist
discipline used for the native diagnostics (no URLs, paths or free text).
"""
import pytest

from mycelium_a13_macos.managed_runtime import _bounded_control_reason

_TOKENS = '[A-Za-z_][A-Za-z0-9_.:]*'


def test_exception_type_and_message_tokens_are_kept():
    reason = _bounded_control_reason(RuntimeError('control_poll_reconciliation_required'))
    assert reason['exception_type'] == 'RuntimeError'
    assert reason['tokens'] == ['control_poll_reconciliation_required']


def test_urls_paths_and_free_text_never_survive():
    reason = _bounded_control_reason(
        RuntimeError('HTTP 500 from https://relay.example.com:443/very/long/path?q=1 boom!')
    )
    blob = repr(reason)
    # No URL or path form can survive: tokens cannot contain '/', '?' or '!'.
    assert '://' not in blob
    assert '/' not in blob
    assert '?' not in blob and '!' not in blob
    # The diagnostic value is kept: the class and a bare host token.
    assert reason['exception_type'] == 'RuntimeError'
    assert any('relay' in token for token in reason['tokens'])


def test_reason_is_bounded():
    reason = _bounded_control_reason(RuntimeError(' '.join('tok%d' % i for i in range(50))))
    assert len(reason['tokens']) <= 4
    assert all(len(token) <= 64 for token in reason['tokens'])


def test_duplicate_tokens_are_deduplicated():
    reason = _bounded_control_reason(RuntimeError('same same same other'))
    assert reason['tokens'] == ['same', 'other']


def test_message_without_any_token_yields_an_empty_list():
    reason = _bounded_control_reason(RuntimeError('!!! ??? ---'))
    assert reason['tokens'] == []
    assert reason['exception_type'] == 'RuntimeError'


@pytest.mark.parametrize(
    'exc,expected',
    [
        (ValueError('x'), 'ValueError'),
        (KeyError('k'), 'KeyError'),
        (TimeoutError('t'), 'TimeoutError'),
        (OSError('o'), 'OSError'),
    ],
)
def test_exception_class_is_always_preserved(exc, expected):
    assert _bounded_control_reason(exc)['exception_type'] == expected
