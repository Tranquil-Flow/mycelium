"""A5 replica-track selector (pure, isolated).

A5 consumes A4 dispatch authority; it does not mint or extend it. This module
is the read-only projection that turns a list of already-validated
``mycelium.replica_qualification.v1`` documents into a per-request set of
``excluded_placement_ids`` for an A4 dispatcher to honor.

Rules (spec §2, §5, §10):
- The selector only reads. It never executes a request, never extends the
  Router's cancellation budget, never mints a new dispatch authority, never
  narrows or widens A4's terminal-state vocabulary.
- A replica track is selectable only when its ``replica_qualification.v1``
  has ``route_ready=True`` and ``expires_at_unix_ms > now_unix_ms``.
- A replica track is unselectable after a replica-loss event for any
  placement it carries: the surviving set stays selectable for the surviving
  placements only.
- Per-request selection is bounded: the caller asks for one ``track_id`` or
  ``"auto"``; the selector returns the corresponding excluded set
  (placements NOT on the chosen track relative to the current
  ``ExecutionGraph``).
- Generation fencing: a replica qualification with
  ``qualifier_generation <= 0`` is rejected as stale.
- Decisions are deterministic for a given (qualifications, current_placement_ids,
  now_unix_ms, replica_loss_placement_ids) tuple.

This module is also the place where the dispatch policy digest lives
(spec §5: ``a4_concurrency_track_policy_digest``). The policy is bound as a
literal string, exactly the way A3 binds other policy digests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from mycelium_replica_contracts import validate_replica_qualification


# Spec §5: only the A5 dispatcher may choose one of N qualified tracks per
# request. The track-policy digest is the discriminated binding A4
# ``route_alive`` already exposes; this string is the only selector policy
# currently authorized. The dispatcher is fail-closed: a different policy
# means no replica track is selectable.
REPLICA_TRACK_POLICY_DIGEST = "a4_concurrency_track_policy_digest"

# A4 liveness states whose subject's placements are treated as replica-lost.
# QUARANTINED and FAILED are the only states where A4 itself fails admission
# closed; SUSPECT still admits, so it is deliberately NOT loss here. A5 only
# reads these states — it never drives a transition into them.
LIVENESS_LOST_STATES = frozenset({"quarantined", "failed"})


def placements_lost_by_liveness(
    placement_nodes: Iterable[tuple[str, str]],
    liveness_states: Mapping[str, str],
) -> frozenset[str]:
    """Placement ids on nodes whose A4 liveness state is quarantined/failed.

    A5 consumes A4's liveness authority without extending it: the A4 detector
    decides when a subject is quarantined or failed; this helper only reads
    the resulting states and marks the placements on such nodes as lost, so
    their replica tracks stop being selectable. SUSPECT placements are NOT
    lost (A4 still admits them), unknown states and malformed inputs are
    ignored, and the result is a deterministic frozenset.
    """

    states = {
        node_id: state
        for node_id, state in liveness_states.items()
        if isinstance(node_id, str) and node_id and isinstance(state, str)
    }
    return frozenset(
        placement_id
        for placement_id, node_id in placement_nodes
        if isinstance(placement_id, str)
        and placement_id
        and isinstance(node_id, str)
        and states.get(node_id) in LIVENESS_LOST_STATES
    )


@dataclass(frozen=True)
class ReplicaTrack:
    """A single selectable replica track derived from a replica qualification."""

    track_id: str
    replica_group_id: str
    placement_id: str
    placement_ids: tuple[str, ...]
    traffic_fraction: float
    qualifier_generation: int
    issued_at_unix_ms: int
    expires_at_unix_ms: int
    workload_envelope_digest: str
    qualification_digest: str


@dataclass(frozen=True)
class TrackSelection:
    """What one request will pass to the A4 dispatcher."""

    track_id: str
    excluded_placements: frozenset[str]
    placement_ids: frozenset[str]
    qualifier_generation: int
    track_policy_digest: str


def _is_selectable(
    quals: Sequence[Mapping[str, Any]],
    *,
    now_unix_ms: int,
    replica_loss_placement_ids: frozenset[str],
    deployment_id: str,
    deployment_epoch: int,
) -> list[ReplicaTrack]:
    """Filter replica qualifications to the currently selectable set."""

    if (
        not isinstance(now_unix_ms, int)
        or isinstance(now_unix_ms, bool)
        or now_unix_ms < 0
    ):
        raise ValueError("invalid_now_unix_ms")

    if not isinstance(deployment_id, str) or not deployment_id:
        raise ValueError("invalid_deployment_id")
    if type(deployment_epoch) is not int or deployment_epoch < 0:
        raise ValueError("invalid_deployment_epoch")

    out: list[ReplicaTrack] = []
    for raw_document in quals:
        document = validate_replica_qualification(raw_document)
        if not isinstance(document, Mapping):
            raise ValueError("invalid_qualification_document")
        if document.get("protocol") != "mycelium.replica_qualification.v1":
            raise ValueError("invalid_qualification_protocol")
        if document.get("route_ready") is not True:
            continue
        if (
            document.get("deployment_id") != deployment_id
            or document.get("deployment_epoch") != deployment_epoch
        ):
            continue
        qualifier_generation = document.get("qualifier_generation")
        if (
            not isinstance(qualifier_generation, int)
            or isinstance(qualifier_generation, bool)
            or qualifier_generation <= 0
        ):
            continue
        issued_at = document.get("issued_at_unix_ms")
        expires_at = document.get("expires_at_unix_ms")
        if not isinstance(issued_at, int) or isinstance(issued_at, bool):
            continue
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            continue
        if expires_at <= now_unix_ms:
            continue
        placement_id = document.get("placement_id")
        if not isinstance(placement_id, str) or not placement_id:
            continue
        placement_ids_field = document.get("placement_ids")
        if isinstance(placement_ids_field, Sequence) and not isinstance(
            placement_ids_field, (str, bytes)
        ):
            placement_ids = tuple(
                str(item)
                for item in placement_ids_field
                if isinstance(item, str) and item
            )
        else:
            placement_ids = (placement_id,)
        if any(item in replica_loss_placement_ids for item in placement_ids):
            # A replica track is unselectable after a replica-loss event for
            # ANY placement it carries (spec §5): the whole track is lost,
            # not just the one placement.
            continue
        track_id = document.get("track_id")
        if not isinstance(track_id, str) or not track_id:
            continue
        replica_group_id = document.get("replica_group_id")
        if not isinstance(replica_group_id, str) or not replica_group_id:
            continue
        workload_envelope_digest = document.get("workload_envelope_digest")
        if (
            not isinstance(workload_envelope_digest, str)
            or not workload_envelope_digest
        ):
            continue
        qualification_digest = document.get("qualification_digest")
        if not isinstance(qualification_digest, str) or not qualification_digest:
            continue
        traffic_fraction = document.get("traffic_fraction")
        if (
            not isinstance(traffic_fraction, (int, float))
            or isinstance(traffic_fraction, bool)
            or not 0.0 < float(traffic_fraction) <= 1.0
        ):
            continue
        out.append(
            ReplicaTrack(
                track_id=track_id,
                replica_group_id=replica_group_id,
                placement_id=placement_id,
                placement_ids=placement_ids,
                traffic_fraction=float(traffic_fraction),
                qualifier_generation=qualifier_generation,
                issued_at_unix_ms=issued_at,
                expires_at_unix_ms=expires_at,
                workload_envelope_digest=workload_envelope_digest,
                qualification_digest=qualification_digest,
            )
        )
    return out


def selectable_replica_tracks(
    replica_qualifications: Iterable[Mapping[str, Any]],
    *,
    now_unix_ms: int,
    deployment_id: str | None = None,
    deployment_epoch: int | None = None,
    replica_loss_placement_ids: Iterable[str] = (),
) -> tuple[ReplicaTrack, ...]:
    """Public: the sorted, deterministic list of currently selectable tracks.

    Sorted by ``(track_id, placement_id)`` so all callers see the same order.
    """
    if not isinstance(replica_qualifications, Iterable) or isinstance(
        replica_qualifications, (str, bytes)
    ):
        raise ValueError("invalid_replica_qualifications")
    documents = list(replica_qualifications)
    if deployment_id is None or deployment_epoch is None:
        identities = {
            (document["deployment_id"], document["deployment_epoch"])
            for raw in documents
            for document in (validate_replica_qualification(raw),)
        }
        if len(identities) != 1:
            raise ValueError("deployment_identity_required")
        inferred_id, inferred_epoch = next(iter(identities))
        deployment_id = inferred_id if deployment_id is None else deployment_id
        deployment_epoch = inferred_epoch if deployment_epoch is None else deployment_epoch
    loss = frozenset(
        item for item in replica_loss_placement_ids if isinstance(item, str) and item
    )
    selectable = _is_selectable(
        documents,
        now_unix_ms=now_unix_ms,
        replica_loss_placement_ids=loss,
        deployment_id=deployment_id,
        deployment_epoch=deployment_epoch,
    )
    selectable.sort(key=lambda track: (track.track_id, track.placement_id))
    identities: dict[str, tuple[tuple[str, ...], float, str]] = {}
    for track in selectable:
        identity = (
            track.placement_ids,
            track.traffic_fraction,
            track.qualification_digest,
        )
        previous = identities.setdefault(track.track_id, identity)
        if previous != identity:
            raise ValueError("conflicting_replica_track_authority")
    selectable = [
        track
        for index, track in enumerate(selectable)
        if index == 0 or track.track_id != selectable[index - 1].track_id
    ]
    return tuple(selectable)


def select_track(
    replica_qualifications: Iterable[Mapping[str, Any]],
    *,
    current_placement_ids: Iterable[str],
    requested_track_id: str | None,
    now_unix_ms: int,
    deployment_id: str | None = None,
    deployment_epoch: int | None = None,
    replica_loss_placement_ids: Iterable[str] = (),
) -> TrackSelection:
    """Public: pick one track for one request and produce the excluded set.

    Args:
      replica_qualifications: the live list of ``replica_qualification.v1``
        documents (already validated).
      current_placement_ids: the placement IDs the current A4
        ``ExecutionGraph`` exposes. The selector returns excluded placements
        relative to THIS set; placements that no longer exist are not
        excluded.
      requested_track_id: a specific ``track_id`` to honor, or ``None`` for
        ``"auto"`` (the selector's first currently-selectable track).
      now_unix_ms: monotonic wall-clock for expiry checks.
      replica_loss_placement_ids: placements the caller has marked as lost
        (replica-loss events); their tracks are unselectable.

    Returns:
      A populated ``TrackSelection``. The caller passes
      ``excluded_placements`` to the A4 dispatcher.

    Raises:
      ValueError: if no track is selectable, the requested track is not
        selectable, or inputs are malformed.
    """
    if isinstance(requested_track_id, str) and not requested_track_id:
        raise ValueError("invalid_requested_track_id")
    selectable = selectable_replica_tracks(
        replica_qualifications,
        now_unix_ms=now_unix_ms,
        deployment_id=deployment_id,
        deployment_epoch=deployment_epoch,
        replica_loss_placement_ids=replica_loss_placement_ids,
    )
    if not selectable:
        raise ValueError("no_qualified_replica_track")
    if requested_track_id is None:
        chosen = selectable[0]
    else:
        match = [track for track in selectable if track.track_id == requested_track_id]
        if not match:
            raise ValueError("requested_track_not_selectable")
        # On a tie, pick the lowest ``placement_id`` so deterministic.
        match.sort(key=lambda track: track.placement_id)
        chosen = match[0]

    current_set = frozenset(
        item for item in current_placement_ids if isinstance(item, str) and item
    )
    chosen_set = frozenset(chosen.placement_ids)
    if not chosen_set or not chosen_set <= current_set:
        raise ValueError("qualified_track_not_current")
    excluded = current_set - chosen_set
    return TrackSelection(
        track_id=chosen.track_id,
        excluded_placements=excluded,
        placement_ids=chosen_set,
        qualifier_generation=chosen.qualifier_generation,
        track_policy_digest=REPLICA_TRACK_POLICY_DIGEST,
    )


__all__ = [
    "REPLICA_TRACK_POLICY_DIGEST",
    "LIVENESS_LOST_STATES",
    "ReplicaTrack",
    "TrackSelection",
    "placements_lost_by_liveness",
    "select_track",
    "selectable_replica_tracks",
]
