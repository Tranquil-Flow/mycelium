# SPDX-License-Identifier: AGPL-3.0-only
"""Which relay errors are worth retrying inside a bounded reconnect budget.

Split out of round14_bootstrap_compile so the policy is testable without that
module's ssh/rpc imports. Import only this to reason about retry behaviour.

The distinction the policy draws: an error that means "not reachable yet" or
"not valid yet, and the next attempt mints a fresh statement" is retryable; an
error that means "the product refused this work" fails closed and is reported
as-is.
"""
TRANSIENT_REACH=('seed_http_unreachable','peer_transport_unreachable','seed_http_remote_error')
# A window rejection is NOT a product refusal. `identity()` re-mints the
# statement fresh on every request with a 60 s window (coordinator.identity_envelope,
# ttl self._message_ttl_seconds), and the endpoint card that authorises the attempt
# is signed for 300 s. So one rejection is the most retryable outcome on this path
# and must not discard the budget. Measured 2026-09-16: 65r1 died 17 s into a 180 s
# budget on `seed_http_seed_time_invalid` at attempt 1, with ~283 s left on the
# card -- the card was then re-minted, and the budget was gone for nothing.
RETRYABLE_WINDOW=('seed_http_seed_time_invalid',)
RETRYABLE=TRANSIENT_REACH+RETRYABLE_WINDOW


def matching_codes(error_text):
    """The retryable codes present in an error message, in policy order."""
    text=str(error_text)
    return [code for code in RETRYABLE if code in text]


def is_retryable(error_text,*,budget_exhausted):
    """Whether another attempt is allowed for this error.

    Budget exhaustion always stops: a persistent rejection must surface rather
    than loop for the life of the card.
    """
    if budget_exhausted:return False
    return bool(matching_codes(error_text))
