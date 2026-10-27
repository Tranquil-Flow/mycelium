# SPDX-License-Identifier: AGPL-3.0-only
"""Chat expiry errors retain partial answers and explain how to recover."""
import pytest
from tests.a13_macos.test_demo_chat import _finished


@pytest.mark.parametrize('code', ['token_path_policy_expired', 'token_path_process_expired', 'managed_control_expired'])
def test_expired_run_explains_next_step_without_discarding_text(code):
    text, empty = _finished([['Partial answer', code], ['', code]])
    assert text.startswith('Partial answer\n[stopped:')
    for value in (text, empty):
        assert 'Start' in value and 'owner' in value
        assert code not in value
