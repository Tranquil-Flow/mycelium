# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_router.rtt_measurement — measured round-trip times for link costing.

Why this module exists
----------------------
Before it, no live RTT sample was filed anywhere:

* ``mycelium_live/route.py::_idle_probe_peer`` issues a real, bounded ``health``
  round trip on every keepalive window but discarded the duration.
* ``mycelium_router/scoring.py::RouteScorer._transfer_ms`` silently substituted
  ``RouterConfig.default_rtt_ms`` (100.0) for a missing neighbour RTT, so a
  *default* was indistinguishable from a *measurement* at the call site.
* ``mycelium_m16_runtime.py::build_live_m16_runtime`` hardcoded ``1.0`` ms for
  every neighbour, which reads like a very fast link.

This module makes the provenance of every RTT value explicit.  A caller can
still get a number for an unmeasured link, but it is labelled — ``measured``,
``fallback_default_ms`` or ``fallback_configured_ms`` — and carries
``sample_count == 0`` when it is not a measurement.  Nothing here ever invents a
sample, a timestamp, or a wall-clock stamp.

Conventions
-----------
``rtt_ms`` is a **full round trip**, matching the consumers that already divide
it by two for a one-way estimate (``mycelium_router/scoring.py`` and
``mycelium_layer_planner/network_cost.py``).  ``latest_ms`` is what
``rtt_ms()`` returns; ``mean_ms`` / ``min_ms`` / ``jitter_ms`` are exposed for
analysis.  A sample is the span a transport actually bracketed around a real
socket write and the matching read completion, so it includes the peer's
processing time and is therefore an upper bound on pure network RTT — that is
why ``measurement_source`` is always recorded alongside the number.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Final, Mapping

from mycelium_layer_planner.contracts import DirectedLinkObservation

#: The documented fallback used when a link has never been measured and the
#: caller supplied no configured value.  Equal to
#: ``mycelium_router.contracts.RouterConfig.default_rtt_ms`` on purpose: the
#: numeric behaviour of the pre-existing silent default is preserved while its
#: provenance stops being silent.
DEFAULT_FALLBACK_RTT_MS: Final[float] = 100.0

#: Provenance labels.  A caller that sees anything other than
#: :data:`PROVENANCE_MEASURED` is looking at a placeholder, not a measurement.
PROVENANCE_MEASURED: Final[str] = "measured"
PROVENANCE_FALLBACK_DEFAULT: Final[str] = "fallback_default_ms"
PROVENANCE_FALLBACK_CONFIGURED: Final[str] = "fallback_configured_ms"

#: Measurement sources.  ``socket_round_trip`` brackets a real socket write and
#: the matching read completion; ``live_session_round_trip`` brackets a live
#: node-session command write and its correlated response.
SOURCE_SOCKET_ROUND_TRIP: Final[str] = "socket_round_trip"
SOURCE_LIVE_SESSION_ROUND_TRIP: Final[str] = "live_session_round_trip"

#: Source key for samples filed by the route's own host, whose stage-node id is
#: not part of the route's closed contract.  A caller that knows the local node
#: id can map it with ``MeasuredRttTable(local_source_alias=...)`` instead of
#: guessing one.
LOCAL_HOST_KEY: Final[str] = "<local-host>"

#: A sample older than this (monotonic seconds, in-process) is exported as a
#: stale link observation so the planner can apply its own confidence discount.
DEFAULT_STALE_AFTER_SECONDS: Final[float] = 30.0


@dataclass(frozen=True)
class RttSample:
    """One resolved link latency, with the provenance of the number."""

    rtt_ms: float
    provenance: str
    sample_count: int
    measurement_source: str | None
    latest_ms: float | None
    mean_ms: float | None
    min_ms: float | None
    jitter_ms: float | None
    last_measured_at_monotonic: float | None
    age_seconds: float | None

    @property
    def is_measured(self) -> bool:
        return self.provenance == PROVENANCE_MEASURED and self.sample_count > 0


@dataclass(frozen=True)
class _LinkHistory:
    samples: tuple[float, ...]
    source: str
    last_measured_at: float

    @property
    def latest(self) -> float:
        return self.samples[-1]

    @property
    def mean(self) -> float:
        return sum(self.samples) / len(self.samples)

    @property
    def minimum(self) -> float:
        return min(self.samples)

    @property
    def jitter(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        mean = self.mean
        variance = sum((value - mean) ** 2 for value in self.samples) / len(
            self.samples
        )
        return math.sqrt(variance)


class MeasuredRttTable:
    """Thread-safe table of round-trip times measured by real transports.

    Keys are directed ``(source_node_id, destination_node_id)`` pairs, because
    a link is only measured in the direction the traffic actually flowed.
    """

    __slots__ = ("_lock", "_links", "_local_alias", "_max_samples_per_link")

    def __init__(
        self,
        *,
        local_source_alias: str | None = None,
        max_samples_per_link: int = 64,
    ) -> None:
        if local_source_alias is not None and not isinstance(local_source_alias, str):
            raise ValueError("local_source_alias must be a string or None")
        if not isinstance(max_samples_per_link, int) or max_samples_per_link < 1:
            raise ValueError("max_samples_per_link must be a positive integer")
        self._lock = threading.Lock()
        self._links: dict[tuple[str, str], _LinkHistory] = {}
        self._local_alias = local_source_alias
        self._max_samples_per_link = max_samples_per_link

    # -- recording --------------------------------------------------------

    def record_round_trip_ms(
        self,
        source_node_id: str,
        destination_node_id: str,
        round_trip_ms: float,
        *,
        measurement_source: str = SOURCE_SOCKET_ROUND_TRIP,
        observed_at: float | None = None,
    ) -> RttSample:
        """File one measured round trip.  Rejects anything not a real duration."""

        for name, value in (
            ("source_node_id", source_node_id),
            ("destination_node_id", destination_node_id),
            ("measurement_source", measurement_source),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(round_trip_ms, bool) or not isinstance(
            round_trip_ms, (int, float)
        ):
            raise ValueError("round_trip_ms must be a number")
        round_trip_ms = float(round_trip_ms)
        if math.isnan(round_trip_ms) or math.isinf(round_trip_ms):
            raise ValueError("round_trip_ms must be finite")
        if round_trip_ms < 0:
            raise ValueError("round_trip_ms must be non-negative")
        stamp = time.monotonic() if observed_at is None else float(observed_at)
        key = (source_node_id, destination_node_id)
        with self._lock:
            previous = self._links.get(key)
            samples = () if previous is None else previous.samples
            samples = (samples + (round_trip_ms,))[-self._max_samples_per_link:]
            self._links[key] = _LinkHistory(
                samples=samples,
                source=measurement_source,
                last_measured_at=stamp,
            )
            history = self._links[key]
        return self._sample(key, history)

    # -- reading ----------------------------------------------------------

    def rtt_ms(
        self,
        source_node_id: str,
        destination_node_id: str,
        *,
        fallback_ms: float | None = None,
    ) -> RttSample:
        """Resolve a link latency, labelling a fallback as a fallback."""

        if fallback_ms is not None and (
            isinstance(fallback_ms, bool)
            or not isinstance(fallback_ms, (int, float))
            or not math.isfinite(float(fallback_ms))
            or float(fallback_ms) < 0
        ):
            raise ValueError("fallback_ms must be a finite non-negative number")
        with self._lock:
            history = self._measured_history(source_node_id, destination_node_id)
        if history is not None:
            return self._sample((source_node_id, destination_node_id), history)
        if fallback_ms is not None:
            return RttSample(
                rtt_ms=float(fallback_ms),
                provenance=PROVENANCE_FALLBACK_CONFIGURED,
                sample_count=0,
                measurement_source=None,
                latest_ms=None,
                mean_ms=None,
                min_ms=None,
                jitter_ms=None,
                last_measured_at_monotonic=None,
                age_seconds=None,
            )
        return RttSample(
            rtt_ms=DEFAULT_FALLBACK_RTT_MS,
            provenance=PROVENANCE_FALLBACK_DEFAULT,
            sample_count=0,
            measurement_source=None,
            latest_ms=None,
            mean_ms=None,
            min_ms=None,
            jitter_ms=None,
            last_measured_at_monotonic=None,
            age_seconds=None,
        )

    def is_measured(self, source_node_id: str, destination_node_id: str) -> bool:
        with self._lock:
            return (
                self._measured_history(source_node_id, destination_node_id)
                is not None
            )

    def bind_local_source_alias(self, node_id: str) -> None:
        """Name the local host's stage-node id so its own samples resolve.

        The live route files its probes under :data:`LOCAL_HOST_KEY` because its
        closed contract does not name its own stage node.  A component that does
        know that id (the supervisor that built the plan) binds it here, after
        which measurements taken by the local host resolve for the route cost
        model.  Nothing is guessed: an unbound table simply leaves those samples
        addressed by ``LOCAL_HOST_KEY``.
        """

        if not isinstance(node_id, str) or not node_id:
            raise ValueError("node_id must be a non-empty string")
        with self._lock:
            self._local_alias = node_id

    def measured_link_count(self) -> int:
        with self._lock:
            return len(self._links)

    def neighbor_rtt_map(self, node_id: str) -> dict[str, float]:
        """Measured RTTs for every destination this node has been observed on.

        Only sampled directions are returned: an unmeasured neighbour is absent
        rather than present with a placeholder, so a consumer can tell the
        difference between "measured as fast" and "never measured".
        """

        with self._lock:
            measured = {
                destination: history.latest
                for (source, destination), history in self._links.items()
                if source == node_id
            }
            if self._local_alias == node_id:
                measured.update(
                    {
                        destination: history.latest
                        for (source, destination), history in self._links.items()
                        if source == LOCAL_HOST_KEY
                    }
                )
        return measured

    def snapshot(self) -> dict[str, object]:
        """A JSON-safe view for evidence payloads."""

        with self._lock:
            links = {
                f"{source}->{destination}": {
                    "latest_ms": history.latest,
                    "mean_ms": round(history.mean, 6),
                    "min_ms": history.minimum,
                    "jitter_ms": round(history.jitter, 6),
                    "sample_count": len(history.samples),
                    "measurement_source": history.source,
                }
                for (source, destination), history in sorted(self._links.items())
            }
        return {
            "protocol": "mycelium.router.measured_rtt.v1",
            "measured_link_count": len(links),
            "links": links,
            "default_fallback_rtt_ms": DEFAULT_FALLBACK_RTT_MS,
            "local_source_alias": self._local_alias,
        }

    def link_observation(
        self,
        source_node_id: str,
        destination_node_id: str,
        *,
        bandwidth_Bps: float | None = None,
        loss_ratio: float = 0.0,
        geolocation_floor_ms: float = 0.0,
        confidence: float = 1.0,
        max_age_seconds: float | None = DEFAULT_STALE_AFTER_SECONDS,
    ) -> DirectedLinkObservation:
        """Export a measured link as the planner's ``DirectedLinkObservation``.

        Raises :class:`ValueError` when the pair has never been measured: an
        unmeasured link must not be exportable as a measured one.  Use
        ``rtt_ms()`` if a labelled fallback is what the caller wants.
        """

        with self._lock:
            history = self._measured_history(source_node_id, destination_node_id)
        if history is None:
            raise ValueError(
                f"link {source_node_id}->{destination_node_id} has no measurement; "
                "refusing to export a fallback as a measured link"
            )
        age = max(0.0, time.monotonic() - history.last_measured_at)
        stale = bool(
            max_age_seconds is not None and age > float(max_age_seconds)
        )
        return DirectedLinkObservation(
            src=source_node_id,
            dst=destination_node_id,
            rtt_ms=history.latest,
            jitter_ms=history.jitter,
            bandwidth_Bps=bandwidth_Bps,
            loss_ratio=loss_ratio,
            geolocation_floor_ms=geolocation_floor_ms,
            inferred=False,
            stale=stale,
            confidence=confidence,
        )

    def link_diagnostics(
        self, source_node_id: str, destination_node_id: str
    ) -> Mapping[str, object]:
        """Provenance for a pair, for evidence and assertions."""

        sample = self.rtt_ms(source_node_id, destination_node_id)
        return {
            "source_node_id": source_node_id,
            "destination_node_id": destination_node_id,
            "rtt_ms": sample.rtt_ms,
            "provenance": sample.provenance,
            "is_measured": sample.is_measured,
            "sample_count": sample.sample_count,
            "measurement_source": sample.measurement_source,
        }

    # -- internals --------------------------------------------------------

    def _measured_history(
        self, source_node_id: str, destination_node_id: str
    ) -> _LinkHistory | None:
        history = self._links.get((source_node_id, destination_node_id))
        if history is not None or self._local_alias != source_node_id:
            return history
        return self._links.get((LOCAL_HOST_KEY, destination_node_id))

    def _sample(self, key: tuple[str, str], history: _LinkHistory) -> RttSample:
        return RttSample(
            rtt_ms=history.latest,
            provenance=PROVENANCE_MEASURED,
            sample_count=len(history.samples),
            measurement_source=history.source,
            latest_ms=history.latest,
            mean_ms=history.mean,
            min_ms=history.minimum,
            jitter_ms=history.jitter,
            last_measured_at_monotonic=history.last_measured_at,
            age_seconds=max(0.0, time.monotonic() - history.last_measured_at),
        )
