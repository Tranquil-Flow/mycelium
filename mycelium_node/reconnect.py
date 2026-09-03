# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded renewal timing and connectivity-state projection for native agents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


class RenewalPolicyError(ValueError):
    pass


class LeaseConnectivityState(str, Enum):
    ONLINE = "online"
    TEMPORARILY_DISCONNECTED = "temporarily_disconnected"
    LEASE_AT_RISK = "lease_at_risk"
    EXPIRED = "expired"
    QUARANTINED = "quarantined"
    REVOKED = "revoked"


@dataclass(frozen=True)
class RenewalRetryPolicy:
    heartbeat_interval_seconds: float = 30.0
    jitter_fraction: float = 0.15
    reconnect_base_seconds: float = 0.5
    reconnect_max_seconds: float = 15.0
    lease_risk_window_seconds: float = 20.0

    def __post_init__(self) -> None:
        values = (
            self.heartbeat_interval_seconds,
            self.jitter_fraction,
            self.reconnect_base_seconds,
            self.reconnect_max_seconds,
            self.lease_risk_window_seconds,
        )
        if any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in values):
            raise RenewalPolicyError("membership_renewal_policy_invalid")
        if (
            self.heartbeat_interval_seconds <= 0
            or self.jitter_fraction < 0
            or self.jitter_fraction > 0.5
            or self.reconnect_base_seconds <= 0
            or self.reconnect_max_seconds < self.reconnect_base_seconds
            or self.lease_risk_window_seconds <= 0
        ):
            raise RenewalPolicyError("membership_renewal_policy_invalid")

    @staticmethod
    def _unit(value: float) -> float:
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise RenewalPolicyError("membership_renewal_random_invalid")
        return min(1.0, max(0.0, float(value)))

    def heartbeat_delay(self, random_value: float) -> float:
        """Return one bounded symmetric heartbeat interval."""

        centered = (self._unit(random_value) * 2.0) - 1.0
        return self.heartbeat_interval_seconds * (
            1.0 + centered * self.jitter_fraction
        )

    def reconnect_delay(
        self,
        *,
        attempt: int,
        random_value: float,
        now: float,
        lease_expires_at: float,
    ) -> float:
        """Return capped full-jitter backoff without sleeping past the lease."""

        if (
            type(attempt) is not int
            or attempt < 0
            or type(now) not in (int, float)
            or type(lease_expires_at) not in (int, float)
            or not math.isfinite(float(now))
            or not math.isfinite(float(lease_expires_at))
        ):
            raise RenewalPolicyError("membership_renewal_retry_invalid")
        remaining = max(0.0, float(lease_expires_at) - float(now))
        if remaining == 0:
            return 0.0
        exponent = min(attempt, 30)
        ceiling = min(
            self.reconnect_max_seconds,
            self.reconnect_base_seconds * (2**exponent),
            remaining,
        )
        return ceiling * self._unit(random_value)

    def state(
        self,
        *,
        now: float,
        lease_expires_at: float,
        disconnected: bool,
        quarantined: bool = False,
        revoked: bool = False,
    ) -> LeaseConnectivityState:
        if revoked:
            return LeaseConnectivityState.REVOKED
        if quarantined:
            return LeaseConnectivityState.QUARANTINED
        if float(now) >= float(lease_expires_at):
            return LeaseConnectivityState.EXPIRED
        if disconnected and float(lease_expires_at) - float(now) <= self.lease_risk_window_seconds:
            return LeaseConnectivityState.LEASE_AT_RISK
        if disconnected:
            return LeaseConnectivityState.TEMPORARILY_DISCONNECTED
        return LeaseConnectivityState.ONLINE


def transient_renewal_failure(exc: BaseException) -> bool:
    """Classify only retry-safe transport and coordinator availability failures."""

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if (
        getattr(exc, "code", None) == "seed_http_unreachable"
        and getattr(exc, "status", None) is None
    ):
        return True
    status = getattr(exc, "status", None)
    return type(status) is int and status in {408, 425, 429, 500, 502, 503, 504}


__all__ = [
    "LeaseConnectivityState",
    "RenewalPolicyError",
    "RenewalRetryPolicy",
    "transient_renewal_failure",
]
