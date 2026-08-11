from __future__ import annotations

import pytest

from mycelium_node.reconnect import (
    LeaseConnectivityState,
    RenewalPolicyError,
    RenewalRetryPolicy,
    transient_renewal_failure,
)


def test_heartbeat_jitter_is_bounded_and_symmetric() -> None:
    policy = RenewalRetryPolicy(heartbeat_interval_seconds=100, jitter_fraction=0.2)
    assert policy.heartbeat_delay(0.0) == 80
    assert policy.heartbeat_delay(0.5) == 100
    assert policy.heartbeat_delay(1.0) == 120


def test_reconnect_backoff_caps_at_policy_and_lease() -> None:
    policy = RenewalRetryPolicy(reconnect_base_seconds=1, reconnect_max_seconds=8)
    assert policy.reconnect_delay(attempt=0, random_value=1, now=10, lease_expires_at=100) == 1
    assert policy.reconnect_delay(attempt=4, random_value=1, now=10, lease_expires_at=100) == 8
    assert policy.reconnect_delay(attempt=9, random_value=1, now=98, lease_expires_at=100) == 2
    assert policy.reconnect_delay(attempt=1, random_value=1, now=100, lease_expires_at=100) == 0


def test_membership_state_priority_and_lease_risk_are_closed() -> None:
    policy = RenewalRetryPolicy(lease_risk_window_seconds=20)
    assert policy.state(now=10, lease_expires_at=100, disconnected=False) is LeaseConnectivityState.ONLINE
    assert policy.state(now=10, lease_expires_at=100, disconnected=True) is LeaseConnectivityState.TEMPORARILY_DISCONNECTED
    assert policy.state(now=81, lease_expires_at=100, disconnected=True) is LeaseConnectivityState.LEASE_AT_RISK
    assert policy.state(now=100, lease_expires_at=100, disconnected=False) is LeaseConnectivityState.EXPIRED
    assert policy.state(now=10, lease_expires_at=100, disconnected=False, quarantined=True) is LeaseConnectivityState.QUARANTINED
    assert policy.state(now=10, lease_expires_at=100, disconnected=False, quarantined=True, revoked=True) is LeaseConnectivityState.REVOKED


@pytest.mark.parametrize(
    "changes",
    [
        {"heartbeat_interval_seconds": 0},
        {"jitter_fraction": -0.01},
        {"jitter_fraction": 0.51},
        {"reconnect_base_seconds": 0},
        {"reconnect_base_seconds": 2, "reconnect_max_seconds": 1},
        {"lease_risk_window_seconds": 0},
    ],
)
def test_invalid_policy_fails_closed(changes: dict[str, float]) -> None:
    with pytest.raises(RenewalPolicyError, match="membership_renewal_policy_invalid"):
        RenewalRetryPolicy(**changes)


class _HTTPFailure(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status


def test_only_transport_and_availability_failures_are_retryable() -> None:
    assert transient_renewal_failure(TimeoutError()) is True
    assert transient_renewal_failure(ConnectionError()) is True
    assert transient_renewal_failure(_HTTPFailure(503)) is True
    assert transient_renewal_failure(_HTTPFailure(429)) is True
    assert transient_renewal_failure(_HTTPFailure(401)) is False
    assert transient_renewal_failure(_HTTPFailure(409)) is False
    assert transient_renewal_failure(RuntimeError("malformed signed renewal")) is False
