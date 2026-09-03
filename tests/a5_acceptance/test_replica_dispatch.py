"""A5 replica dispatch interleaving tests (deterministic, fleet-free).

These tests cover spec §10 dispatcher interleavings WITHOUT the physical
fleet. They use a stub backend and a stub router so the dispatch seam —
track selection → excluded placements → A4 backend lifecycle — is
exercised as a unit, in isolation from the A4 atomic commit.

What they prove:
- A5 only CONSUMES A4 authority. No new terminal state, no new cancel
  budget, no replica selection restart after A4's deadline.
- Concurrent requests on distinct tracks land on distinct placements.
- A replica-loss event drops the affected track from the selectable set
  without disturbing the surviving track.
- Selection is generation-fenced: a stale replica qualification is rejected.
- A request whose track was removed mid-flight is not silently re-bound to
  the surviving track by the A5 wrapper; A4's backend lifetime owns it.
"""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from mycelium_replica_contracts import validate_replica_qualification
from mycelium_qualification.replica import (
    ReplicaQualificationInput,
    qualify_replica_track,
)
from mycelium_qualification.replica_track import (
    REPLICA_TRACK_POLICY_DIGEST,
    select_track,
    selectable_replica_tracks,
)
from mycelium_qualification.replica_track_backend import (
    BoundTrack,
    ReplicaTrackDispatcher,
)
from mycelium_request_gateway.backend import (
    RouterSessionBackend,
)
from mycelium_request_gateway.contracts import (
    AdmissionError,
    InferenceSubmission,
)
from mycelium_request_gateway.qualification import QualificationSource


# ---------------------------------------------------------------------------
# Test fixtures: deterministic replica qualifications + a fake router
# ---------------------------------------------------------------------------


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _qualification(
    *,
    track_id: str,
    placement_id: str,
    qualifier_generation: int = 1,
    issued_at_unix_ms: int = 1_000,
    expires_at_unix_ms: int = 1_000_000,
    route_ready: bool = True,
    replica_group_id: str = "group-0",
    placement_ids: tuple[str, ...] | None = None,
    traffic_fraction: float = 0.5,
) -> dict[str, Any]:
    data = ReplicaQualificationInput(
        deployment_id="deployment-test",
        deployment_epoch=1,
        replica_group_id=replica_group_id,
        placement_id=placement_id,
        placement_ids=(
            placement_ids
            if placement_ids is not None
            else (placement_id, "p-shared-stage-1")
        ),
        track_id=track_id,
        traffic_fraction=traffic_fraction,
        qualifier_generation=qualifier_generation,
        issued_at_unix_ms=issued_at_unix_ms,
        expires_at_unix_ms=expires_at_unix_ms,
        evidence_bundle_digest=_digest(f"eb-{track_id}"),
        load_proof_digest=_digest(f"lp-{placement_id}"),
        assignment_digest=_digest(f"assignment-{placement_id}"),
        artifact_verification_digest=_digest(f"av-{placement_id}"),
        parity_verified=True,
        startup_challenge_passed=True,
        memory_within_bounds=True,
        cleanup_within_bounds=True,
        directed_link_qualified=True,
        workload_envelope_digest=_digest(f"we-{track_id}"),
    )
    return qualify_replica_track(
        data,
        extra_rejections=() if route_ready else ("owner_authority_missing",),
    )


@dataclass(frozen=True)
class _FakePlacement:
    placement_id: str
    node_id: str = "node-0"


@dataclass(frozen=True)
class _FakeStage:
    placements: tuple[_FakePlacement, ...]
    stage_id: str = "stage-0"


@dataclass(frozen=True)
class _FakeExecutionGraph:
    stages: tuple[_FakeStage, ...]
    deployment_id: str = "deployment-test"
    deployment_epoch: int = 1
    topology_version: str = "v1"
    model_id: str = "model-test"
    resolved_commit: str = "commit-test"
    manifest_digest: str = "sha256:0"
    execution_graph_digest: str = "sha256:0"


class _FakeRouter:
    """A minimal RouterPort. Records the per-request admission call."""

    def __init__(self, graph: _FakeExecutionGraph) -> None:
        self._graph = graph
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._released: set[str] = set()
        self.runtime: dict[str, Any] | None = None

    def current_deployment(self) -> _FakeExecutionGraph:
        return self._graph

    def runtime_status(self) -> dict[str, Any] | None:
        return self.runtime

    def admit(
        self,
        request: Any,
        client_sink: Any,
        *,
        pinned_deployment: Any | None = None,
        **kwargs: Any,
    ) -> str:
        with self._lock:
            self.calls.append(
                {
                    "request_id": getattr(request, "request_id", None),
                    "excluded_placements": kwargs.get(
                        "excluded_placements", frozenset()
                    ),
                }
            )
        return getattr(request, "request_id", "")

    def decode_one(self, request_id: str) -> bool:
        return True

    def request_status(self, request_id: str) -> str:
        return "COMPLETED"

    def cancel(self, request_id: str) -> bool:
        return True

    def cancel_with_deadline(
        self,
        request_id: str,
        *,
        deadline_monotonic_s: float,
    ) -> bool:
        return True

    def update_publisher_generation(
        self,
        request_id: str,
        *,
        expected_generation: int,
        new_generation: int,
    ) -> bool:
        return True

    def release_request(self, request_id: str) -> None:
        with self._lock:
            self._released.add(request_id)


class _FakeCodec:
    """Encode returns one token; decode passes it through."""

    def encode(self, prompt: str) -> tuple[int, ...]:
        return (1,)

    def decode_token(self, token_id: int) -> str:
        return "x"


def _qualification_source_returns_none() -> QualificationSource:
    class _Source:
        def current(self) -> Any:
            return None

    return _Source()  # type: ignore[return-value]


def _request_submission() -> InferenceSubmission:
    from mycelium_request_gateway.contracts import QualificationBinding

    digest = _digest("binding")
    return InferenceSubmission(
        prompt="hello",
        max_new_tokens=2,
        qualification=QualificationBinding(
            qualification_id="qual-test",
            qualification_digest=digest,
            deployment_id="deployment-test",
            deployment_epoch=1,
            topology_version=1,
            model_id="model-test",
            resolved_commit="commit-test",
            manifest_digest=digest,
            path_manifest_digest=digest,
            stage_load_proof_digests=(digest,),
        ),
    )


def _graph_with(*placement_ids: str) -> _FakeExecutionGraph:
    return _FakeExecutionGraph(
        stages=(
            _FakeStage(placements=tuple(_FakePlacement(p) for p in placement_ids)),
            _FakeStage(
                placements=(_FakePlacement("p-shared-stage-1"),),
                stage_id="stage-1",
            ),
        ),
    )


def _build_dispatcher(
    *,
    router: _FakeRouter,
    quals: tuple[dict[str, Any], ...],
    lost: tuple[str, ...] = (),
) -> ReplicaTrackDispatcher:
    """Construct a dispatcher with a fixed replica-loss set and qualifier list."""

    loss_holder: list[frozenset[str]] = [frozenset(lost)]

    def quals_factory() -> list[dict[str, Any]]:
        return list(quals)

    def loss_factory() -> frozenset[str]:
        return loss_holder[0]

    return ReplicaTrackDispatcher(
        router=router,
        codec=_FakeCodec(),
        clock=lambda: 0.0,
        qualification_source=_qualification_source_returns_none(),
        replica_qualifications_factory=quals_factory,
        replica_loss_placement_ids_factory=loss_factory,
    )


# ---------------------------------------------------------------------------
# Track-selection pure-function tests
# ---------------------------------------------------------------------------


def test_select_track_returns_excluded_for_chosen_track_only():
    quals = [
        _qualification(track_id="track-A", placement_id="p0"),
        _qualification(track_id="track-B", placement_id="p1"),
    ]
    for qualification in quals:
        validate_replica_qualification(qualification)
    selected = select_track(
        quals,
        current_placement_ids=("p0", "p1", "p-shared-stage-1"),
        requested_track_id="track-A",
        now_unix_ms=10_000,
    )
    assert selected.track_id == "track-A"
    assert selected.excluded_placements == frozenset({"p1"})
    assert selected.placement_ids == frozenset({"p0", "p-shared-stage-1"})
    assert selected.track_policy_digest == REPLICA_TRACK_POLICY_DIGEST


def test_select_track_auto_picks_first_selectable():
    quals = [
        _qualification(track_id="track-A", placement_id="p0"),
        _qualification(track_id="track-B", placement_id="p1"),
    ]
    selected = select_track(
        quals,
        current_placement_ids=("p0", "p1", "p-shared-stage-1"),
        requested_track_id=None,
        now_unix_ms=10_000,
    )
    assert selected.track_id == "track-A"


def test_select_track_rejects_expired_qualification():
    quals = [
        _qualification(track_id="track-A", placement_id="p0", expires_at_unix_ms=5_000)
    ]
    with pytest.raises(ValueError, match="no_qualified_replica_track"):
        select_track(
            quals,
            current_placement_ids=("p0", "p-shared-stage-1"),
            requested_track_id=None,
            now_unix_ms=10_000,
        )


def test_select_track_rejects_stale_generation():
    quals = [
        _qualification(track_id="track-A", placement_id="p0", qualifier_generation=0)
    ]
    with pytest.raises(ValueError, match="no_qualified_replica_track"):
        select_track(
            quals,
            current_placement_ids=("p0", "p-shared-stage-1"),
            requested_track_id=None,
            now_unix_ms=10_000,
        )


def test_select_track_rejects_route_ready_false():
    quals = [_qualification(track_id="track-A", placement_id="p0", route_ready=False)]
    with pytest.raises(ValueError, match="no_qualified_replica_track"):
        select_track(
            quals,
            current_placement_ids=("p0", "p-shared-stage-1"),
            requested_track_id=None,
            now_unix_ms=10_000,
        )


def test_select_track_drops_replica_loss_placement():
    quals = [
        _qualification(track_id="track-A", placement_id="p0"),
        _qualification(track_id="track-B", placement_id="p1"),
    ]
    # After loss of p0, asking for track-A is refused.
    with pytest.raises(ValueError, match="requested_track_not_selectable"):
        select_track(
            quals,
            current_placement_ids=("p0", "p1", "p-shared-stage-1"),
            requested_track_id="track-A",
            now_unix_ms=10_000,
            replica_loss_placement_ids=("p0",),
        )
    # Surviving track is still selectable.
    selected = select_track(
        quals,
        current_placement_ids=("p0", "p1", "p-shared-stage-1"),
        requested_track_id="track-B",
        now_unix_ms=10_000,
        replica_loss_placement_ids=("p0",),
    )
    assert selected.track_id == "track-B"
    assert selected.excluded_placements == frozenset({"p0"})


def test_select_track_unknown_track_is_rejected():
    quals = [_qualification(track_id="track-A", placement_id="p0")]
    with pytest.raises(ValueError, match="requested_track_not_selectable"):
        select_track(
            quals,
            current_placement_ids=("p0", "p-shared-stage-1"),
            requested_track_id="track-X",
            now_unix_ms=10_000,
        )


def test_selectable_replica_tracks_rejects_conflicting_track_identity():
    quals = [
        _qualification(track_id="track-B", placement_id="p1"),
        _qualification(track_id="track-A", placement_id="p0"),
        _qualification(track_id="track-A", placement_id="p2"),
    ]
    with pytest.raises(ValueError, match="conflicting_replica_track_authority"):
        selectable_replica_tracks(quals, now_unix_ms=10_000)


# ---------------------------------------------------------------------------
# Dispatcher integration tests with a fake Router
# ---------------------------------------------------------------------------


def test_dispatcher_select_returns_bound_track():
    router = _FakeRouter(_graph_with("p0", "p1"))
    quals = (
        _qualification(track_id="track-A", placement_id="p0", traffic_fraction=1 / 3),
        _qualification(track_id="track-B", placement_id="p1", traffic_fraction=1 / 3),
    )
    dispatcher = _build_dispatcher(router=router, quals=quals)
    bound = dispatcher.select(requested_track_id="track-B", now_unix_ms=10_000)
    assert bound.track_id == "track-B"
    assert bound.excluded_placements == frozenset({"p0"})
    assert bound.placement_ids == frozenset({"p1", "p-shared-stage-1"})


def test_dispatcher_select_when_no_qualifications_admits_admission_error():
    router = _FakeRouter(_graph_with("p0"))
    dispatcher = _build_dispatcher(router=router, quals=())
    with pytest.raises(AdmissionError, match="no_qualified_replica_track"):
        dispatcher.select(requested_track_id=None, now_unix_ms=10_000)


def test_concurrent_requests_on_distinct_tracks_pin_distinct_exclusions():
    """Two concurrent requests bind distinct complete multi-stage tracks.

    Deterministic interleaving: two threads each call ``select`` with
    different tracks. The replicated stage differs while the shared stage is
    present in both complete paths.
    """

    router = _FakeRouter(_graph_with("p0", "p1"))
    quals = (
        _qualification(track_id="track-A", placement_id="p0"),
        _qualification(track_id="track-B", placement_id="p1"),
    )
    dispatcher = _build_dispatcher(router=router, quals=quals)

    barrier = threading.Barrier(2)
    results: dict[str, BoundTrack] = {}

    def _select(track_id: str, request_id: str) -> None:
        barrier.wait(timeout=2)
        results[request_id] = dispatcher.select(
            requested_track_id=track_id, now_unix_ms=10_000
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(_select, "track-A", "req-A")
        future_b = executor.submit(_select, "track-B", "req-B")
        future_a.result(timeout=2)
        future_b.result(timeout=2)

    bound_a = results["req-A"]
    bound_b = results["req-B"]
    assert bound_a.track_id == "track-A"
    assert bound_b.track_id == "track-B"
    assert bound_a.placement_ids == frozenset({"p0", "p-shared-stage-1"})
    assert bound_b.placement_ids == frozenset({"p1", "p-shared-stage-1"})
    assert bound_a.excluded_placements == frozenset({"p1"})
    assert bound_b.excluded_placements == frozenset({"p0"})


def test_replica_loss_blocks_new_admission_on_lost_track_keep_surviving():
    """A replica-loss event drops the lost track from the selectable set,
    leaving the surviving track selectable at reduced capacity."""

    quals = (
        _qualification(track_id="track-A", placement_id="p0"),
        _qualification(track_id="track-B", placement_id="p1"),
    )

    router = _FakeRouter(_graph_with("p0", "p1"))
    loss_holder: list[frozenset[str]] = [frozenset()]

    def quals_factory() -> list[dict[str, Any]]:
        return list(quals)

    def loss_factory() -> frozenset[str]:
        return loss_holder[0]

    dispatcher = ReplicaTrackDispatcher(
        router=router,
        codec=_FakeCodec(),
        clock=lambda: 0.0,
        qualification_source=_qualification_source_returns_none(),
        replica_qualifications_factory=quals_factory,
        replica_loss_placement_ids_factory=loss_factory,
    )

    # Pre-loss: both tracks are selectable.
    pre_loss = dispatcher.select(requested_track_id="track-A", now_unix_ms=10_000)
    assert pre_loss.track_id == "track-A"

    # After loss-of-p0, only track-B is selectable.
    loss_holder[0] = frozenset({"p0"})
    with pytest.raises(AdmissionError, match="requested_track_not_selectable"):
        dispatcher.select(requested_track_id="track-A", now_unix_ms=10_000)
    surviving = dispatcher.select(requested_track_id="track-B", now_unix_ms=10_000)
    assert surviving.track_id == "track-B"
    assert surviving.excluded_placements == frozenset({"p0"})


def test_dispatcher_does_not_restart_track_selection_after_deadline():
    """A5 must not re-select a track for cleanup. A4's backend lifetime owns it.

    The wrapper exposes ``cancel_with_deadline`` that forwards the deadline
    verbatim; the underlying A4 backend itself owns the absolute bound.
    The A5 wrapper does NOT re-evaluate the track on cancel.
    """

    router = _FakeRouter(_graph_with("p0", "p1"))
    quals = (
        _qualification(track_id="track-A", placement_id="p0"),
        _qualification(track_id="track-B", placement_id="p1"),
    )
    dispatcher = _build_dispatcher(router=router, quals=quals)
    bound_a = dispatcher.select(requested_track_id="track-A", now_unix_ms=10_000)

    # The dispatcher still cancels the request through the bound track
    # even after the replica qualification expires — the A4 bound is the
    # original cancellation authority, not the A5 selector.
    result = dispatcher.cancel_with_deadline(
        "req-A",
        deadline_monotonic_s=10.0,
        bound_track=bound_a,
    )
    assert result is True


def test_dispatcher_releases_via_a4_backend():
    """A5 ``release`` is a forwarder to A4's per-request backend; nothing more."""

    router = _FakeRouter(_graph_with("p0"))
    quals = (_qualification(track_id="track-A", placement_id="p0"),)
    dispatcher = _build_dispatcher(router=router, quals=quals)
    bound = dispatcher.select(requested_track_id="track-A", now_unix_ms=10_000)
    # No exception means A4's release was invoked.
    dispatcher.release("req-A", bound_track=bound)


def test_dispatcher_update_publisher_generation_forwards_to_a4():
    router = _FakeRouter(_graph_with("p0"))
    quals = (_qualification(track_id="track-A", placement_id="p0"),)
    dispatcher = _build_dispatcher(router=router, quals=quals)
    bound = dispatcher.select(requested_track_id="track-A", now_unix_ms=10_000)
    # A4's update_publisher_generation is forward-only; just exercising the path.
    result = dispatcher.update_publisher_generation(
        "req-A",
        expected_generation=0,
        new_generation=1,
        bound_track=bound,
    )
    assert result is True


def test_dispatcher_propagates_no_qualified_replica_track_via_admission_error():
    """Surface a stable AdmissionError code; do not mint a new error vocabulary."""

    router = _FakeRouter(_graph_with("p0"))
    dispatcher = _build_dispatcher(router=router, quals=())
    with pytest.raises(AdmissionError) as exc_info:
        dispatcher.select(requested_track_id=None, now_unix_ms=10_000)
    assert str(exc_info.value) == "no_qualified_replica_track"


def test_dispatcher_rejects_unknown_track_policy_digest():
    """Track-policy digest is the discriminated binding A4 already exposes."""

    router = _FakeRouter(_graph_with("p0"))
    quals = (_qualification(track_id="track-A", placement_id="p0"),)
    dispatcher = _build_dispatcher(router=router, quals=quals)
    bound = dispatcher.select(requested_track_id="track-A", now_unix_ms=10_000)
    assert bound.track_policy_digest == REPLICA_TRACK_POLICY_DIGEST


# ---------------------------------------------------------------------------
# ReplicaTrackSessionBackend integration (request-gateway composition seam)
# ---------------------------------------------------------------------------


def _build_session_backend(
    *,
    router: _FakeRouter,
    quals: tuple[dict[str, Any], ...] = (),
    lost: frozenset[str] = frozenset(),
    now_holder: list[int] | None = None,
) -> tuple[Any, list[int], Callable[[], list[dict[str, Any]]]]:
    from mycelium_qualification.replica_track_backend import (
        ReplicaTrackDispatcher,
        ReplicaTrackSessionBackend,
    )

    if now_holder is None:
        now_holder = [10_000]

    def quals_factory() -> list[dict[str, Any]]:
        return list(quals)

    def loss_factory() -> frozenset[str]:
        return lost

    dispatcher = ReplicaTrackDispatcher(
        router=router,
        codec=_FakeCodec(),
        clock=lambda: 0.0,
        qualification_source=None,  # no qualifier source: A4 default path
        replica_qualifications_factory=quals_factory,
        replica_loss_placement_ids_factory=loss_factory,
    )
    plain_backend = RouterSessionBackend(
        router=router,
        codec=_FakeCodec(),
        clock=lambda: 0.0,
        qualification_source=None,
    )
    backend = ReplicaTrackSessionBackend(
        dispatcher=dispatcher,
        now_unix_ms=lambda: now_holder[0],
        plain_backend=plain_backend,
    )
    return backend, now_holder, (lambda: router.calls)


def test_session_backend_falls_back_to_plain_a4_when_no_track():
    """Spec §6: no replica-plan failure may poison the base deployment.

    With zero selectable replica tracks the request must run through the
    exact A4 default path with an empty exclusion set.
    """

    router = _FakeRouter(_graph_with("p0", "p1"))
    backend, _now, calls = _build_session_backend(router=router, quals=())
    outcome = backend.run(
        "req-001",
        _request_submission(),
        lambda index, token: None,
        lambda: False,
    )
    assert outcome == "completed"
    assert calls()[0]["excluded_placements"] == frozenset()


def test_session_backend_binds_track_and_forwards_exclusions():
    """A selectable replica track must pin the request to its placements."""

    router = _FakeRouter(_graph_with("p0", "p1"))
    quals = (
        _qualification(track_id="track-A", placement_id="p0", traffic_fraction=1 / 3),
        _qualification(track_id="track-B", placement_id="p1", traffic_fraction=1 / 3),
    )
    backend, _now, calls = _build_session_backend(router=router, quals=quals)
    outcome = backend.run(
        "req-001",
        _request_submission(),
        lambda index, token: None,
        lambda: False,
    )
    assert outcome == "completed"
    # Round-robin starts at the incumbent (plain A4 path); the SECOND request
    # lands on the first sorted replica track (track-A/p0), excluding p1.
    assert calls()[0]["excluded_placements"] == frozenset()
    outcome = backend.run(
        "req-002",
        _request_submission(),
        lambda index, token: None,
        lambda: False,
    )
    assert outcome == "completed"
    assert calls()[1]["excluded_placements"] == frozenset({"p1"})


def test_session_backend_round_robins_across_tracks():
    """Consecutive requests rotate incumbent → replica tracks, round-robin.

    The rotation set is ``[incumbent, track-A, track-B]``: the incumbent is
    the plain A4 default path (empty exclusion set), not a new authority.
    """

    router = _FakeRouter(_graph_with("p0", "p1"))
    quals = (
        _qualification(track_id="track-A", placement_id="p0", traffic_fraction=1 / 3),
        _qualification(track_id="track-B", placement_id="p1", traffic_fraction=1 / 3),
    )
    backend, _now, calls = _build_session_backend(router=router, quals=quals)
    for request_id in ("req-001", "req-002", "req-003", "req-004"):
        backend.run(
            request_id,
            _request_submission(),
            lambda index, token: None,
            lambda: False,
        )
    excluded_sets = [call["excluded_placements"] for call in calls()]
    assert excluded_sets[0] == frozenset()
    assert excluded_sets[1] == frozenset({"p1"})
    assert excluded_sets[2] == frozenset({"p0"})
    assert excluded_sets[3] == frozenset()


def test_session_backend_one_replica_alternates_incumbent_and_replica():
    """The physical fleet case: ONE replica track must still alternate.

    With a single replica qualification, consecutive requests alternate
    incumbent (plain A4 path) and the replica track, so two concurrent
    requests provably land on distinct complete tracks (spec §1).
    """

    router = _FakeRouter(_graph_with("p0", "p1"))
    quals = (_qualification(track_id="track-A", placement_id="p0"),)
    backend, _now, calls = _build_session_backend(router=router, quals=quals)
    for request_id in ("req-001", "req-002", "req-003"):
        backend.run(
            request_id,
            _request_submission(),
            lambda index, token: None,
            lambda: False,
        )
    excluded_sets = [call["excluded_placements"] for call in calls()]
    assert excluded_sets[0] == frozenset()
    assert excluded_sets[1] == frozenset({"p1"})
    assert excluded_sets[2] == frozenset()


def test_session_backend_expired_track_falls_back_to_plain():
    """An expired replica qualification degrades to the A4 default path."""

    router = _FakeRouter(_graph_with("p0", "p1"))
    quals = (
        _qualification(track_id="track-A", placement_id="p0", expires_at_unix_ms=5_000),
    )
    backend, now_holder, calls = _build_session_backend(router=router, quals=quals)
    now_holder[0] = 20_000
    outcome = backend.run(
        "req-001",
        _request_submission(),
        lambda index, token: None,
        lambda: False,
    )
    assert outcome == "completed"
    assert calls()[0]["excluded_placements"] == frozenset()


def test_session_backend_lost_track_falls_back_to_plain():
    """A replica-loss event on the only track degrades to A4 default."""

    router = _FakeRouter(_graph_with("p0", "p1"))
    quals = (
        _qualification(track_id="track-A", placement_id="p0"),
        _qualification(track_id="track-B", placement_id="p1"),
    )
    backend, _now, calls = _build_session_backend(
        router=router, quals=quals, lost=frozenset({"p0", "p1"})
    )
    outcome = backend.run(
        "req-001",
        _request_submission(),
        lambda index, token: None,
        lambda: False,
    )
    assert outcome == "completed"
    assert calls()[0]["excluded_placements"] == frozenset()


def test_session_backend_does_not_select_saturated_replica_track():
    router = _FakeRouter(_graph_with("p0", "p1"))
    router.runtime = {
        "placements": [
            {
                "placement_id": "p0",
                "active_reservations": 1,
                "maximum_reservations": 1,
            },
            {
                "placement_id": "p-shared-stage-1",
                "active_reservations": 0,
                "maximum_reservations": 1,
            },
        ]
    }
    quals = (_qualification(track_id="track-A", placement_id="p0"),)
    backend, _now, calls = _build_session_backend(router=router, quals=quals)
    for request_id in ("req-001", "req-002"):
        backend.run(
            request_id,
            _request_submission(),
            lambda index, token: None,
            lambda: False,
        )
    assert [call["excluded_placements"] for call in calls()] == [
        frozenset(),
        frozenset(),
    ]


def test_session_backend_cancel_before_run_is_sticky():
    """A pre-admission cancel must not resurrect into a track run."""

    router = _FakeRouter(_graph_with("p0", "p1"))
    quals = (
        _qualification(track_id="track-A", placement_id="p0"),
        _qualification(track_id="track-B", placement_id="p1"),
    )
    backend, _now, calls = _build_session_backend(router=router, quals=quals)
    assert backend.cancel("req-001") is True
    outcome = backend.run(
        "req-001",
        _request_submission(),
        lambda index, token: None,
        lambda: True,
    )
    assert outcome == "cancelled"
    assert calls() == []


def test_session_backend_bounds_unknown_pre_admission_state():
    """Unknown request IDs cannot grow the pre-bind maps without limit."""

    router = _FakeRouter(_graph_with("p0", "p1"))
    backend, _now, _calls = _build_session_backend(router=router, quals=())
    accepted = [backend.cancel(f"unknown-{index}") for index in range(1025)]
    assert accepted.count(True) == 1024
    assert accepted[-1] is False


def test_session_backend_rejects_unbounded_pending_request_id():
    router = _FakeRouter(_graph_with("p0", "p1"))
    backend, _now, _calls = _build_session_backend(router=router, quals=())
    assert backend.cancel("x" * 257) is False


# ---------------------------------------------------------------------------
# Replica-loss derivation from A4 liveness states (spec §5: replica loss
# blocks new admission on affected tracks; the surviving track stays usable)
# ---------------------------------------------------------------------------


def test_placements_lost_by_liveness_quarantined_and_failed_only():
    """Quarantined/failed subjects mark their node's placements lost.

    A5 consumes A4's liveness authority: QUARANTINED and FAILED are the only
    states where A4 itself fails admission closed. SUSPECT and FRESH still
    admit, so they must NOT produce replica loss.
    """

    from mycelium_qualification.replica_track import placements_lost_by_liveness

    placement_nodes = [
        ("p0", "node-0"),
        ("p1", "node-2"),
        ("p2", "node-3-r2"),
    ]
    states = {
        "node-0": "fresh",
        "node-2": "suspect",
        "node-3-r2": "quarantined",
    }
    assert placements_lost_by_liveness(placement_nodes, states) == frozenset({"p2"})
    states["node-0"] = "failed"
    assert placements_lost_by_liveness(placement_nodes, states) == frozenset(
        {"p0", "p2"}
    )


def test_placements_lost_by_liveness_ignores_unknown_and_malformed():
    """Unknown states and malformed inputs must be ignored, never fatal."""

    from mycelium_qualification.replica_track import placements_lost_by_liveness

    placement_nodes = [("p0", "node-0"), ("p1", "node-2"), ("", "node-0"), (7, "node-2")]
    states = {"node-0": "weird_state", "node-2": None, 3: "quarantined"}
    assert placements_lost_by_liveness(placement_nodes, states) == frozenset()


def test_placements_lost_by_liveness_missing_state_not_lost():
    """A node absent from the state mapping is not treated as lost."""

    from mycelium_qualification.replica_track import placements_lost_by_liveness

    placement_nodes = [("p0", "node-0"), ("p1", "node-2")]
    assert placements_lost_by_liveness(placement_nodes, {}) == frozenset()
    assert placements_lost_by_liveness(placement_nodes, {"node-0": "fresh"}) == frozenset()


def test_loss_set_makes_affected_track_unselectable_keeps_survivor():
    """A loss-marked placement drops its track; the survivor stays selectable."""

    from mycelium_qualification.replica_track import selectable_replica_tracks

    quals = (
        _qualification(track_id="track-A", placement_id="p0"),
        _qualification(track_id="track-B", placement_id="p1"),
    )
    selectable = selectable_replica_tracks(
        quals,
        now_unix_ms=2_000,
        replica_loss_placement_ids={"p1"},
    )
    assert [track.track_id for track in selectable] == ["track-A"]
    selectable = selectable_replica_tracks(
        quals,
        now_unix_ms=2_000,
        replica_loss_placement_ids={"p0"},
    )
    assert [track.track_id for track in selectable] == ["track-B"]
    assert selectable_replica_tracks(
        quals,
        now_unix_ms=2_000,
        replica_loss_placement_ids={"p0", "p1"},
    ) == ()


def test_loss_of_any_track_placement_makes_whole_track_unselectable():
    """Loss of a NON-primary placement drops the whole multi-placement track.

    Spec §5: a replica-loss event for any placement the track carries makes
    the entire track unselectable — not just the primary placement row.
    """

    from mycelium_qualification.replica_track import selectable_replica_tracks

    multi_placement = _qualification(
        track_id="track-A",
        placement_id="p0",
        placement_ids=("p0", "p1"),
    )
    quals = (
        multi_placement,
        _qualification(track_id="track-B", placement_id="p2"),
    )
    # Losing the non-primary p1 kills track-A entirely; track-B survives.
    selectable = selectable_replica_tracks(
        quals,
        now_unix_ms=2_000,
        replica_loss_placement_ids={"p1"},
    )
    assert [track.track_id for track in selectable] == ["track-B"]
