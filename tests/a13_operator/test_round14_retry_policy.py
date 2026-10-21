# SPDX-License-Identifier: AGPL-3.0-only
"""The reconnect retry policy, tested without the operator's ssh/rpc imports.

2026-09-16 65r1: reconnect_target retried only TRANSIENT_REACH codes, so a single
`seed_http_seed_time_invalid` at attempt 1 discarded the whole 180 s budget while
the authorising card still had ~283 s left. A window rejection is retryable by
construction -- `identity()` re-mints a fresh 60 s statement on every request --
so it must not end the budget. A genuine product refusal still fails closed.
"""
import importlib.util
import pathlib

POLICY = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator/round14_retry_policy.py'
spec = importlib.util.spec_from_file_location('round14_retry_policy', POLICY)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def test_a_window_rejection_is_retryable():
    text = 'local_api_http_409:seed_http_seed_time_invalid'
    assert policy.matching_codes(text) == ['seed_http_seed_time_invalid']
    assert policy.is_retryable(text, budget_exhausted=False) is True


def test_reach_failures_stay_retryable():
    for code in ('seed_http_unreachable', 'peer_transport_unreachable', 'seed_http_remote_error'):
        assert policy.is_retryable(code, budget_exhausted=False) is True


def test_a_product_refusal_still_fails_closed():
    # Not a reach problem and not a re-mintable window: report as-is.
    for text in ('managed_control_reconciliation_required',
                 'local_api_http_409:seed_http_seed_pin_mismatch',
                 'peer_seed_identity_mismatch',
                 'endpoint_recovery_invalid'):
        assert policy.matching_codes(text) == []
        assert policy.is_retryable(text, budget_exhausted=False) is False


def test_budget_exhaustion_always_stops():
    text = 'local_api_http_409:seed_http_seed_time_invalid'
    assert policy.is_retryable(text, budget_exhausted=True) is False


def test_the_window_code_is_the_only_addition_over_the_reach_set():
    assert policy.RETRYABLE_WINDOW == ('seed_http_seed_time_invalid',)
    assert policy.RETRYABLE == policy.TRANSIENT_REACH + policy.RETRYABLE_WINDOW
    assert 'seed_http_seed_time_invalid' not in policy.TRANSIENT_REACH
