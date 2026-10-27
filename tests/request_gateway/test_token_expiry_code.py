# SPDX-License-Identifier: AGPL-3.0-only
"""Closed token-lifetime errors cross the gateway exception boundary."""
import pytest
from test_router_error_code import FailingRouter, terminal_code


@pytest.mark.parametrize('code', ['token_path_policy_expired', 'token_path_process_expired'])
def test_token_lifetime_refusal_is_not_generic_backend_failure(code):
    class RefusingRouter(FailingRouter):
        def admit(self, *args, **kwargs):
            raise RuntimeError(code)
    assert terminal_code(RefusingRouter(None)) == ('failed', code)


def test_other_exception_text_stays_private():
    class RefusingRouter(FailingRouter):
        def admit(self, *args, **kwargs):
            raise RuntimeError('private_but_syntactically_safe')
    assert terminal_code(RefusingRouter(None)) == ('failed', 'backend_failed')
