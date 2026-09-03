"""A5 replica-track dispatcher wrapper (delegates to A4).

A5 consumes A4 dispatch authority; the wrapper below lets the A4 request
gateway choose a replica track on a per-request basis without modifying A4
in any way. The wrapper is A5 code:

- It reads the live A5 replica_qualification.v1 list and the live
  ExecutionGraph's placement IDs.
- It computes the per-request ``excluded_placements`` set via
  :mod:`mycelium_qualification.replica_track`.
- It builds a per-request A4 ``RouterSessionBackend`` bound to that
  exclusion set and forwards ``run`` / ``cancel`` / ``cancel_with_deadline``
  / ``release`` / ``update_publisher_generation`` unchanged.

What this wrapper does NOT do (so the brief's "single touch" rule holds):
- It does not define a new cancellation budget. It forwards the deadline
  argument verbatim; the A4 backend owns the absolute 2,000 ms bound.
- It does not introduce a new terminal status. A4's terminal vocabulary
  (completed / cancelled / failed) is the only one returned.
- It does not restart replica selection after A4's deadline. Once a
  request is admitted, the A4 backend's lifecycle owns the request — the
  wrapper does not re-select a track for cancellation cleanup.
- It does not touch A4's abandoned-subscriber behavior. The A4 fix
  (discarded_through vs acknowledged_through) is a gateway-side concern
  that the wrapper does not duplicate or shadow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Callable, Iterable, Mapping

from mycelium_request_gateway.backend import (
    PromptCodec,
    RouterPort,
    RouterSessionBackend,
)
from mycelium_request_gateway.contracts import (
    AdmissionError,
    InferenceSubmission,
)
from mycelium_request_gateway.qualification import QualificationSource

from .replica_track import (
    REPLICA_TRACK_POLICY_DIGEST,
    TrackSelection,
    select_track,
    selectable_replica_tracks,
)

_MAX_PENDING_REQUESTS = 1024


@dataclass(frozen=True)
class BoundTrack:
    """A snapshot of the dispatch decision for one request.

    The underlying A4 backend is constructed once per ``select`` call and
    stored inside the bound track. ``run`` / ``cancel`` / ``release`` /
    ``update_publisher_generation`` all forward to that SAME backend
    instance. A4's backend is therefore the exclusive owner of the request's
    cancellation, cleanup, and publisher-generation state; A5 does not keep
    shadow lifecycle state or re-select a track after admission.
    """

    track_id: str
    track_policy_digest: str
    qualifier_generation: int
    excluded_placements: frozenset[str]
    placement_ids: frozenset[str]
    _backend: RouterSessionBackend = field(repr=False, compare=False)


def _live_placement_ids(deployment: Any) -> tuple[str, ...]:
    """Extract the placement IDs from the live A4 ExecutionGraph."""

    if deployment is None:
        raise AdmissionError("qualification_unavailable")
    stages = getattr(deployment, "stages", None)
    if not stages:
        raise AdmissionError("qualification_unavailable")
    out: list[str] = []
    for stage in stages:
        for placement in getattr(stage, "placements", ()):
            placement_id = getattr(placement, "placement_id", None)
            if isinstance(placement_id, str) and placement_id:
                out.append(placement_id)
    return tuple(out)


class ReplicaTrackDispatcher:
    """A5 dispatcher that delegates execution to A4 ``RouterSessionBackend``.

    The dispatcher is constructed ONCE per service lifetime and called once
    per request. ``replica_qualifications_factory`` is the live list of
    ``replica_qualification.v1`` documents; ``replica_loss_placement_ids_factory``
    is the live replica-loss set. ``router`` and ``codec`` are passed through
    to the A4 per-request backend.
    """

    def __init__(
        self,
        *,
        router: RouterPort,
        codec: PromptCodec,
        clock: Callable[[], float],
        qualification_source: QualificationSource | None,
        replica_qualifications_factory: Callable[[], Iterable[Mapping[str, Any]]],
        replica_loss_placement_ids_factory: Callable[[], frozenset[str]],
        sampling_seed: int = 0,
    ) -> None:
        if not callable(replica_qualifications_factory):
            raise ValueError("invalid_replica_qualifications_factory")
        if not callable(replica_loss_placement_ids_factory):
            raise ValueError("invalid_replica_loss_placement_ids_factory")
        self._router = router
        self._codec = codec
        self._clock = clock
        self._qualification_source = qualification_source
        self._replica_qualifications_factory = replica_qualifications_factory
        self._replica_loss_placement_ids_factory = replica_loss_placement_ids_factory
        if not isinstance(sampling_seed, int) or isinstance(sampling_seed, bool):
            raise ValueError("invalid_sampling_seed")
        self._sampling_seed = sampling_seed

    def select(
        self,
        *,
        requested_track_id: str | None,
        now_unix_ms: int,
    ) -> BoundTrack:
        """Return the dispatch decision for one request.

        The caller MAY pass the returned ``BoundTrack`` to ``run`` /
        ``cancel`` / ``release`` / ``update_publisher_generation`` to drive
        the request's lifecycle. The A5 wrapper does not remember the
        track; A4's backend is the source of truth for cancellation and
        cleanup.

        Raises ``AdmissionError`` (not a new error code) when no replica
        track is selectable. The caller may either surface this to the user
        or fall back to the A4 default (no A5 replica exclusion).
        """
        deployment = self._router.current_deployment()
        current_placement_ids = _live_placement_ids(deployment)
        deployment_id = getattr(deployment, "deployment_id", None)
        deployment_epoch = getattr(deployment, "deployment_epoch", None)
        if not isinstance(deployment_id, str) or type(deployment_epoch) is not int:
            raise AdmissionError("qualification_unavailable")
        try:
            decision: TrackSelection = select_track(
                list(self._replica_qualifications_factory()),
                current_placement_ids=current_placement_ids,
                requested_track_id=requested_track_id,
                now_unix_ms=now_unix_ms,
                deployment_id=deployment_id,
                deployment_epoch=deployment_epoch,
                replica_loss_placement_ids=self._replica_loss_placement_ids_factory(),
            )
        except ValueError as exc:
            raise AdmissionError(str(exc)) from exc
        if decision.track_policy_digest != REPLICA_TRACK_POLICY_DIGEST:
            raise AdmissionError("track_policy_digest_unknown")
        for stage in deployment.stages:
            stage_ids = {
                placement.placement_id
                for placement in getattr(stage, "placements", ())
                if isinstance(getattr(placement, "placement_id", None), str)
            }
            if len(stage_ids & decision.placement_ids) != 1:
                raise AdmissionError("qualified_track_incomplete")
        backend = self._build_backend(decision)
        return BoundTrack(
            track_id=decision.track_id,
            track_policy_digest=decision.track_policy_digest,
            qualifier_generation=decision.qualifier_generation,
            excluded_placements=decision.excluded_placements,
            placement_ids=decision.placement_ids,
            _backend=backend,
        )

    def _build_backend(
        self, track: TrackSelection | BoundTrack
    ) -> RouterSessionBackend:
        return RouterSessionBackend(
            router=self._router,
            codec=self._codec,
            clock=self._clock,
            qualification_source=self._qualification_source,
            excluded_placements=track.excluded_placements,
            sampling_seed=self._sampling_seed,
            selected_placements=track.placement_ids,
        )

    def run(
        self,
        request_id: str,
        submission: InferenceSubmission,
        emit_token: Callable[[int, str], None],
        is_cancelled: Callable[[], bool],
        *,
        bound_track: BoundTrack,
    ) -> str:
        """Forward run to A4 with the per-request exclusion set."""

        return bound_track._backend.run(
            request_id, submission, emit_token, is_cancelled
        )

    def cancel(self, request_id: str, *, bound_track: BoundTrack) -> bool:
        """Forward cancel to A4. Track selection is NOT re-evaluated."""

        return bound_track._backend.cancel(request_id)

    def cancel_with_deadline(
        self,
        request_id: str,
        *,
        deadline_monotonic_s: float,
        bound_track: BoundTrack,
    ) -> bool:
        """Forward the A4 deadline argument verbatim. A4 owns the budget."""

        return bound_track._backend.cancel_with_deadline(
            request_id, deadline_monotonic_s=deadline_monotonic_s
        )

    def release(self, request_id: str, *, bound_track: BoundTrack) -> None:
        bound_track._backend.release(request_id)

    def update_publisher_generation(
        self,
        request_id: str,
        *,
        expected_generation: int,
        new_generation: int,
        bound_track: BoundTrack,
    ) -> bool:
        return bound_track._backend.update_publisher_generation(
            request_id,
            expected_generation=expected_generation,
            new_generation=new_generation,
        )


class ReplicaTrackSessionBackend:
    """Request-gateway backend that selects one A5 replica track per request.

    This implements the same public methods as A4 ``RouterSessionBackend`` and
    delegates each request's lifecycle to the A4 backend bound inside its
    ``BoundTrack``. It owns only the request_id -> BoundTrack association so
    cancel/release/update reach the same A4 backend instance selected at
    admission time.

    When no replica track is currently selectable, each request runs through
    the injected ``plain_backend`` — the exact A4 default path. A missing,
    expired, or lost replica qualification therefore degrades to A4 behavior
    instead of poisoning the base deployment (spec §6).
    """

    def __init__(
        self,
        *,
        dispatcher: ReplicaTrackDispatcher,
        now_unix_ms: Callable[[], int],
        plain_backend: RouterSessionBackend,
    ) -> None:
        if not callable(now_unix_ms):
            raise ValueError("invalid_replica_time_source")
        self._dispatcher = dispatcher
        self._now_unix_ms = now_unix_ms
        self._plain_backend = plain_backend
        self._lock = threading.RLock()
        self._bound: dict[str, BoundTrack] = {}
        self._plain_used: set[str] = set()
        self._pending_deadlines: dict[str, float | None] = {}
        self._pending_generations: dict[str, int] = {}
        self._rr_cursor = 0
        self._weighted_current: dict[str | None, float] = {}

    def _pending_slot_available(self, request_id: str) -> bool:
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id.encode("utf-8")) > 256
        ):
            return False
        pending = set(self._pending_deadlines) | set(self._pending_generations)
        return request_id in pending or len(pending) < _MAX_PENDING_REQUESTS

    def _take_pending(self, request_id: str) -> tuple[bool, float | None, int]:
        cancelled = request_id in self._pending_deadlines
        deadline = self._pending_deadlines.pop(request_id, None)
        generation = self._pending_generations.pop(request_id, 1)
        return cancelled, deadline, generation

    @staticmethod
    def _apply_pending(
        backend: RouterSessionBackend,
        request_id: str,
        *,
        cancelled: bool,
        deadline: float | None,
        pending_generation: int,
    ) -> None:
        if cancelled:
            if deadline is None:
                backend.cancel(request_id)
            else:
                cancel_with_deadline = getattr(backend, "cancel_with_deadline", None)
                if callable(cancel_with_deadline):
                    cancel_with_deadline(
                        request_id, deadline_monotonic_s=deadline
                    )
                else:
                    backend.cancel(request_id)
        if pending_generation > 1:
            current = 1
            while current < pending_generation:
                if not backend.update_publisher_generation(
                    request_id,
                    expected_generation=current,
                    new_generation=current + 1,
                ):
                    raise AdmissionError("publisher_generation_sync_failed")
                current += 1

    def _choose_track_id(self, now_unix_ms: int) -> str | None:
        """Rotate over the incumbent A4 default path plus every replica track.

        ``None`` names the incumbent: the plain A4 ``RouterSessionBackend``
        with no exclusions — the already-authorized default path, not a new
        authority A5 mints. With one replica track the rotation alternates
        incumbent/replica, so concurrent requests provably land on distinct
        complete tracks (spec §1/§5). When no replica track is selectable
        the rotation degenerates to ``[None]`` and every request uses the
        incumbent (reduced capacity, spec §5 replica loss).
        """

        tracks = selectable_replica_tracks(
            self._dispatcher._replica_qualifications_factory(),  # noqa: SLF001 - same module seam
            now_unix_ms=now_unix_ms,
            deployment_id=(
                self._dispatcher._router.current_deployment().deployment_id  # noqa: SLF001
            ),
            deployment_epoch=(
                self._dispatcher._router.current_deployment().deployment_epoch  # noqa: SLF001
            ),
            replica_loss_placement_ids=(
                self._dispatcher._replica_loss_placement_ids_factory()  # noqa: SLF001
            ),
        )
        runtime_status = getattr(self._dispatcher._router, "runtime_status", None)  # noqa: SLF001
        status = runtime_status() if callable(runtime_status) else None
        if isinstance(status, Mapping):
            placements = status.get("placements")
            if isinstance(placements, list):
                available = {
                    item.get("placement_id")
                    for item in placements
                    if isinstance(item, Mapping)
                    and isinstance(item.get("placement_id"), str)
                    and type(item.get("active_reservations")) is int
                    and type(item.get("maximum_reservations")) is int
                    and item["active_reservations"] < item["maximum_reservations"]
                }
                known = {
                    item.get("placement_id")
                    for item in placements
                    if isinstance(item, Mapping)
                    and isinstance(item.get("placement_id"), str)
                }
                tracks = tuple(
                    track
                    for track in tracks
                    if all(
                        placement_id not in known or placement_id in available
                        for placement_id in track.placement_ids
                    )
                )
        replica_fraction = sum(track.traffic_fraction for track in tracks)
        if replica_fraction > 1.0 + 1e-9:
            # Contradictory allocation authority must not poison the incumbent.
            tracks = ()
            replica_fraction = 0.0
        weights: dict[str | None, float] = {
            None: max(0.0, 1.0 - replica_fraction),
            **{track.track_id: track.traffic_fraction for track in tracks},
        }
        weights = {option: weight for option, weight in weights.items() if weight > 0.0}
        if not weights:
            weights = {None: 1.0}
        with self._lock:
            self._weighted_current = {
                option: self._weighted_current.get(option, 0.0) + weight
                for option, weight in weights.items()
            }
            chosen = max(
                self._weighted_current,
                key=lambda option: self._weighted_current[option],
            )
            self._weighted_current[chosen] -= sum(weights.values())
            self._rr_cursor += 1
        return chosen

    def _bind(self, request_id: str) -> BoundTrack | None:
        now = self._now_unix_ms()
        if not isinstance(now, int) or isinstance(now, bool) or now < 0:
            raise AdmissionError("invalid_replica_time_source")
        track_id = self._choose_track_id(now)
        if track_id is None:
            return None
        bound = self._dispatcher.select(
            requested_track_id=track_id,
            now_unix_ms=now,
        )
        with self._lock:
            self._bound[request_id] = bound
            cancelled, deadline, pending_generation = self._take_pending(request_id)
        self._apply_pending(
            bound._backend,
            request_id,
            cancelled=cancelled,
            deadline=deadline,
            pending_generation=pending_generation,
        )
        return bound

    def run(
        self,
        request_id: str,
        submission: InferenceSubmission,
        emit_token: Callable[[int, str], None],
        is_cancelled: Callable[[], bool],
    ) -> str:
        if is_cancelled():
            return "cancelled"
        bound = self._bind(request_id)
        if bound is None:
            with self._lock:
                self._plain_used.add(request_id)
                cancelled, deadline, pending_generation = self._take_pending(request_id)
            self._apply_pending(
                self._plain_backend,
                request_id,
                cancelled=cancelled,
                deadline=deadline,
                pending_generation=pending_generation,
            )
            return self._plain_backend.run(
                request_id, submission, emit_token, is_cancelled
            )
        return self._dispatcher.run(
            request_id,
            submission,
            emit_token,
            is_cancelled,
            bound_track=bound,
        )

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            bound = self._bound.get(request_id)
            plain = request_id in self._plain_used
            if bound is None and not plain:
                if not self._pending_slot_available(request_id):
                    return False
                self._pending_deadlines[request_id] = None
                return True
        if bound is not None:
            return self._dispatcher.cancel(request_id, bound_track=bound)
        return self._plain_backend.cancel(request_id)

    def cancel_with_deadline(
        self,
        request_id: str,
        *,
        deadline_monotonic_s: float,
    ) -> bool:
        with self._lock:
            bound = self._bound.get(request_id)
            plain = request_id in self._plain_used
            if bound is None and not plain:
                if not self._pending_slot_available(request_id):
                    return False
                self._pending_deadlines[request_id] = deadline_monotonic_s
                return True
        if bound is not None:
            return self._dispatcher.cancel_with_deadline(
                request_id,
                deadline_monotonic_s=deadline_monotonic_s,
                bound_track=bound,
            )
        cancel_with_deadline = getattr(
            self._plain_backend, "cancel_with_deadline", None
        )
        if callable(cancel_with_deadline):
            return cancel_with_deadline(
                request_id, deadline_monotonic_s=deadline_monotonic_s
            )
        return self._plain_backend.cancel(request_id)

    def release(self, request_id: str) -> None:
        with self._lock:
            bound = self._bound.pop(request_id, None)
            plain = request_id in self._plain_used
            if plain:
                self._plain_used.discard(request_id)
            self._pending_deadlines.pop(request_id, None)
            self._pending_generations.pop(request_id, None)
        if bound is not None:
            self._dispatcher.release(request_id, bound_track=bound)
        elif plain:
            release = getattr(self._plain_backend, "release", None)
            if callable(release):
                release(request_id)

    def update_publisher_generation(
        self,
        request_id: str,
        *,
        expected_generation: int,
        new_generation: int,
    ) -> bool:
        if (
            type(expected_generation) is not int
            or type(new_generation) is not int
            or new_generation != expected_generation + 1
            or expected_generation < 0
        ):
            return False
        with self._lock:
            bound = self._bound.get(request_id)
            plain = request_id in self._plain_used
            if bound is None and not plain:
                if not self._pending_slot_available(request_id):
                    return False
                current = self._pending_generations.get(request_id, 1)
                if expected_generation == 0 and new_generation == 1 and current == 1:
                    self._pending_generations[request_id] = 1
                    return True
                if current != expected_generation:
                    return False
                self._pending_generations[request_id] = new_generation
                return True
        if bound is not None:
            return self._dispatcher.update_publisher_generation(
                request_id,
                expected_generation=expected_generation,
                new_generation=new_generation,
                bound_track=bound,
            )
        return self._plain_backend.update_publisher_generation(
            request_id,
            expected_generation=expected_generation,
            new_generation=new_generation,
        )


__all__ = [
    "BoundTrack",
    "ReplicaTrackDispatcher",
    "ReplicaTrackSessionBackend",
    "REPLICA_TRACK_POLICY_DIGEST",
]
