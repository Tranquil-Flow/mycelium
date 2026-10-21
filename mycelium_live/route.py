# SPDX-License-Identifier: AGPL-3.0-only
"""Lifecycle seam for a physical inference route that outlives one request."""

from __future__ import annotations

from collections import deque
from concurrent.futures import as_completed, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import weakref
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import quote

import mycelium_deadlines
from mycelium_internet.activation import (
    ConnectionEvidence,
    PathClass,
    RelayProjector,
    build_activation_observation,
)

from mycelium_membership.contracts import (
    peer_runtime_is_activation_eligible,
    sign_membership_message,
)
from mycelium_invite import mint_invite_bundle
from mycelium_node.process import (
    PhysicalNodeProcess,
    PrivateDirectoryLease,
    private_directory_lease,
)
from mycelium_seed.authority import (
    PRODUCT_PSEUDONYM_KEY_FILE,
    SeedAuthorityError,
    derive_product_pseudonym_salt,
    load_bound_seed_signer,
    load_product_pseudonym_salt,
)
from mycelium_seed.state import SeedStateError, SqliteSeedState
from mycelium_seed.operator import SeedOperatorError, revoke_seed_member
from mycelium_qualification.signing import Ed25519EvidenceSigner
from mycelium_router.contracts import ExecutionGraph
from mycelium_router.instrument import (
    HopRecorder,
    HopSample,
    MEASURED_RTT_SOURCE_LIVE_SESSION_ROUND_TRIP,
)
from mycelium_router.rtt_measurement import (
    LOCAL_HOST_KEY,
    SOURCE_LIVE_SESSION_ROUND_TRIP,
    MeasuredRttTable,
)
from mycelium_router.serialization import execution_graph_to_dict
from mycelium_topology_evidence import validate_transport_path_observation
from mycelium_router.a10_live_publication import (
    A10PublicationError,
    validate_publication as _validate_a10_publication,
)
from physical_inference_node import execution_graph_from_document
from physical_inference_qualification import (
    ControllerError,
    NODE_COMMAND_TIMEOUT_SECONDS,
    NODE_SESSION_TIMEOUT_SECONDS,
    PeerIdentity,
    QualificationController,
    _NODE_PROCESS_ENV_PREFIX,
    _peer_process_argv,
)
from mycelium_live.liveness import (
    LivenessState,
    LivenessSubject,
    ObservationSource,
    SubjectKind,
    TrafficAwareLivenessDetector,
)


# Every lifecycle/keepalive timing in this block is bound to the deadline
# registry (packets wp5-d9 and wp5-bind-residual): one owner per number, no
# second copy that can drift.  The cleanup fanout window is
# ``cancel_and_cleanup_budget_seconds - cleanup_fallback_reserve_seconds`` and
# the registry owns both inputs and the receipt-poll / inline-receipt-grace /
# reconciliation horizon / worker count / idle-keepalive probe / snapshot-slice
# / transport-margin records, so a peer's advertised cleanup bound
# (``mycelium_deadlines.advertised_maximum_total_cleanup_ms``) can never exceed
# the window this module actually applies at the fanout deadline below - and the
# admission predicate below measures that advertisement against the same
# registry numbers the advertisement is derived from, so the shipped node is
# admitted by its own predicate.
# Module-level assignments on purpose: the existing suites monkeypatch
# ``mycelium_live.route.<NAME>`` by dotted string.
_IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS = mycelium_deadlines.IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS
_CLEANUP_RECEIPT_POLL_SECONDS = mycelium_deadlines.CLEANUP_RECEIPT_POLL_SECONDS
# No `_CLEANUP_SNAPSHOT_ATTEMPT_SECONDS` binding here: wp5-cleanup-levers removed
# it because nothing in this module consumed it and the route's live fallback
# attempt is handed the *measured* slice below (1.1 s), not the pre-a13 0.5 s
# attempt interval - so a bound-but-unread constant made the registry and the
# code disagree about what one attempt is.  The two numbers stay separate records
# on purpose, and `mycelium_deadlines` states the difference:
# `cleanup_fallback_reserve_seconds` is the interval *held back* at the fanout
# boundary and is the number this route applies (`_CLEANUP_FALLBACK_RESERVE_SECONDS`,
# one owner), while `cleanup_snapshot_slice_seconds` is the duration an attempt is
# *handed* once the route resumes it (measured 0.875-1.066 s relayed round trips,
# so 0.5 s was a second way to miss).  The attempt record remains registered as
# the composition term that keeps the reserve honest and is explicitly listed in
# `mycelium_deadlines.NON_BINDING_DEADLINES`.
# One request-scoped fallback snapshot must be able to complete on the real
# relayed path, not merely be dispatched.  Measured on the two-Mac route
# (round 50, request f35736e4-…; see
# `outputs/a13-macos-evidence/round52-cleanup-provable-within-window.md` §2):
# successful snapshot round trips to the relayed laptop were 0.875-1.066 s and
# the first fallback attempt's 0.5 s slice was never even claimed before it
# expired.  A slice shorter than one real round trip is not a recovery path, it
# is a second way to miss.  1.1 s covers the measured maximum with margin and
# still fits the owner's immutable window after the inline grace interval
# (`test_cleanup_snapshot_slice_covers_a_measured_peer_round_trip`).  The
# measured value itself is the registry's
# ``cleanup_snapshot_slice_seconds`` record, not a route-local literal: the
# slice is spent from the owner deadline, and it is bounded by it.
_CLEANUP_SNAPSHOT_SLICE_SECONDS = mycelium_deadlines.CLEANUP_SNAPSHOT_SLICE_SECONDS
# Local, node-suffixed codes that mean "this attempt expired against the
# deadline it was handed", as opposed to an authoritative proof failure.
# `_snapshot_nodes_before` mints `<code>@<node_id>`, so both are matched as
# substrings.  Malformed, unsigned, mismatched or explicitly incomplete proof
# never appears here and stays fail-closed.
_CLEANUP_RETRYABLE_ATTEMPT_CODES = (
    "node_response_timeout@",
    "command_deadline_exceeded@",
)
_CLEANUP_FALLBACK_RESERVE_SECONDS = mycelium_deadlines.CLEANUP_FALLBACK_RESERVE_SECONDS
_CLEANUP_INLINE_RECEIPT_GRACE_SECONDS = mycelium_deadlines.CLEANUP_INLINE_RECEIPT_GRACE_SECONDS
# Reviewer decision 2: a peer may not advertise a cleanup bound the route cannot
# honour.  The admitted maximum is the route's real wait for that advertisement
# (the owner cancellation-and-cleanup budget ``_MAX_CANCELLATION_AND_CLEANUP_SECONDS``
# minus the retry reserve) less a transport margin for
# command delivery plus receipt return.  Measured against decision 3's
# requirement, the two-Mac peer's end-to-end cleanup was **1.83 s / 3.18 s**
# against a 1.5 s window (round52-…md §13), i.e. it does not fit — which is
# exactly the case this predicate must name at startup instead of discovering
# mid-request.  The margin is a registry record
# (``peer_cleanup_bound_transport_margin_ms``) because the advertised bound the
# registry derives is written with it: advertised = window - margin - slack.
PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS = mycelium_deadlines.PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS

# The owner's cancellation-and-cleanup window, in seconds: the ONE record the
# whole composition partitions (``mycelium_deadlines``; re-derived 2026-09-20 from
# the measured peer cleanup 1.83 s / 3.18 s by owner decision, the same pattern as
# the RSS bound b7f0e07).  Every deadline this module mints for a cancellation or a
# request-scoped cleanup is anchored on it: a route-local 2.0 s made the node
# advertise a bound its own owner could never honour, which is precisely the D9
# violation that failed p77r1's smoke request.  Bound from the registry, never
# copied -- ``mycelium_deadlines.BOUND_CONSUMER_BINDINGS`` keeps it from drifting.
_MAX_CANCELLATION_AND_CLEANUP_SECONDS = mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS


def admit_peer_cleanup_bound(
    advertised_ms: object,
    *,
    node_id: str = "unknown",
    owner_budget_ms: int | None = None,
    reserve_ms: int | None = None,
) -> int | None:
    """Return the admitted cleanup bound, or raise a *named* refusal.

    ``None`` means the peer published no bound: an absent assertion is not a
    passing one, but it is also not this predicate's business to invent one, so
    the caller decides.  A present bound must fit the route's fanout window with
    the transport margin, otherwise the peer is refused by name rather than
    allowed to fail closed on a real request.

    The budget, the reserve and the margin default to their registry records
    (``cancel_and_cleanup_budget_seconds``, ``cleanup_fallback_reserve_seconds``,
    ``peer_cleanup_bound_transport_margin_ms``), so this predicate measures every
    peer with the same arithmetic the registry used to derive the bound this
    node advertises (``advertised_maximum_total_cleanup_ms`` = window - margin
    - slack): the shipped node passes its own predicate, and the admission and
    the advertisement cannot drift apart.
    """

    if type(advertised_ms) is not int or isinstance(advertised_ms, bool):
        return None
    budget = (
        int(mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS * 1_000)
        if owner_budget_ms is None
        else owner_budget_ms
    )
    reserve = (
        int(_CLEANUP_FALLBACK_RESERVE_SECONDS * 1_000)
        if reserve_ms is None
        else reserve_ms
    )
    window_ms = budget - reserve
    admitted = window_ms - PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS
    if advertised_ms > admitted:
        raise ValueError(
            f"peer_cleanup_bound_exceeds_route_window:{node_id}:"
            f"advertised={advertised_ms}:admitted={admitted}:window={window_ms}"
        )
    return advertised_ms


_CLEANUP_RECONCILIATION_HORIZON_SECONDS = mycelium_deadlines.CLEANUP_RECONCILIATION_HORIZON_SECONDS
_CLEANUP_RECONCILIATION_WORKERS = mycelium_deadlines.CLEANUP_RECONCILIATION_WORKERS
_NODE_RESPONSE_READER_FAILURE_CODES = frozenset(
    {
        "invalid_node_response",
        "invalid_node_response_frame",
        "node_response_read_failed",
        "response_command_mismatch",
    }
)


def _unknown_activation_projection(now_unix_ms: int) -> dict[str, Any]:
    return {
        "protocol": "mycelium.internet_activation_observation.v1",
        "observation_id": "projection-unknown",
        "connection_generation": 0,
        "connection_reuse": 0,
        "path_class": "unknown",
        "path_source": "unknown",
        "endpoint_pseudonym": None,
        "observed_at_unix_ms": now_unix_ms,
        "freshness": "unknown",
        "evidence_lifetime_until_unix_ms": now_unix_ms + 90_000,
        "metrics": {
            "rtt_ms": None,
            "warm_rtt_ms": None,
            "jitter_ms": None,
            "goodput_bytes_per_second": None,
            "loss_ratio": None,
            "sample_count": None,
            "measured_zero": False,
        },
    }


def _rounded_nonnegative(value: Any) -> int:
    return max(0, int(round(float(value))))


def _product_internet_native_projection(
    *,
    snapshots: Mapping[str, Mapping[str, Any]],
    projection_key: bytes,
    now_unix_ms: int,
    route_available: bool,
    qualification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project verified sidecar path evidence into the closed public A8 shape."""

    observations: list[dict[str, Any]] = []
    for snapshot_node_id, snapshot in snapshots.items():
        details = snapshot.get("details")
        transport = details.get("transport") if isinstance(details, Mapping) else None
        raw = (
            transport.get("transport_path_observations")
            if isinstance(transport, Mapping)
            else None
        )
        if not isinstance(raw, list):
            continue
        for candidate in raw:
            try:
                item = validate_transport_path_observation(
                    candidate,
                    now_unix_ms=now_unix_ms,
                    require_resolved=True,
                )
            except (TypeError, ValueError):
                continue
            if item["local_node_id"] != snapshot_node_id:
                continue
            observations.append(item)

    history: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    if observations:
        lane = min(
            (item["local_node_id"], item["remote_node_id"]) for item in observations
        )
        deduplicated: dict[tuple[int, int, str], dict[str, Any]] = {}
        for item in observations:
            if (item["local_node_id"], item["remote_node_id"]) != lane:
                continue
            fingerprint = (
                item["connection_generation"],
                item["measured_at_unix_ms"],
                item["path_class"],
            )
            deduplicated[fingerprint] = item
        selected = sorted(
            deduplicated.values(),
            key=lambda item: (
                item["measured_at_unix_ms"],
                item["connection_generation"],
                item["path_class"],
            ),
        )[-64:]

    for item in selected:
        metrics = {
            "rtt_ms": _rounded_nonnegative(item["cold_rtt_ms"]),
            "warm_rtt_ms": _rounded_nonnegative(item["warm_rtt_ms"]),
            "jitter_ms": _rounded_nonnegative(item["jitter_ms"]),
            "goodput_bytes_per_second": _rounded_nonnegative(
                item["observed_goodput_Bps"]
            ),
            "loss_ratio": float(item["loss_ratio"]),
            "sample_count": item["sample_count"],
            "measured_zero": float(item["loss_ratio"]) == 0.0,
        }
        evidence = ConnectionEvidence(
            connection_generation=item["connection_generation"],
            connection_reuse=max(0, item["frames_sent"] - item["connections_opened"]),
            path_class=PathClass(item["path_class"]),
            endpoint_id=item["remote_endpoint_id"],
            metrics=metrics,
            observed_at_unix_ms=item["measured_at_unix_ms"],
            relay_identity=item["relay_identity"],
            relay_region=item["relay_region"],
        )
        history.append(
            build_activation_observation(
                evidence=evidence,
                endpoint_pseudonym=(
                    "sha256:"
                    + hashlib.sha256(
                        item["remote_endpoint_id"].encode("utf-8")
                    ).hexdigest()
                ),
                observation_id=(
                    f"transport-{item['connection_generation']}-"
                    f"{item['measured_at_unix_ms']}"
                ),
            )
        )

    current = history[-1] if history else _unknown_activation_projection(now_unix_ms)
    if not history:
        history = [current]
    relay_projection = None
    if selected and current["path_class"] == "relay":
        raw_relay = selected[-1]["relay_identity"]
        if isinstance(raw_relay, str) and raw_relay:
            relay_projection = {
                "protocol": "mycelium.relay_projection.v1",
                "relay_reference": RelayProjector(
                    projection_key=projection_key
                ).reference(raw_relay),
                "region": RelayProjector(projection_key=projection_key).region(
                    selected[-1]["relay_region"], reviewed=False
                ),
                "projection_generation": current["connection_generation"],
                "stable": True,
                "observed_at_unix_ms": current["observed_at_unix_ms"],
            }
    return {
        "bootstrap_status": {
            "protocol": "mycelium.internet_bootstrap_status.v1",
            "generation": 1,
            "observed_at_unix_ms": now_unix_ms,
            "freshness": "current" if route_available else "unknown",
            "tls_state": "unknown",
            "canonical_origin_verified": False,
            "seed_pin_state": "unknown",
            "route_state": "available" if route_available else "unavailable",
            "invitation_state": "unknown",
            "counters": {
                "requests": 0,
                "joins_accepted": 0,
                "joins_rejected": 0,
            },
        },
        "activation_observation": current,
        "activation_history": history,
        "relay_projection": relay_projection,
        "qualification": None if qualification is None else dict(qualification),
    }


def _merge_product_activation_history(
    prior_history: Sequence[Mapping[str, Any]],
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Retain bounded public transition history across sidecar snapshots."""

    detached = json.loads(json.dumps(dict(projection)))
    projected_history = detached["activation_history"]
    by_id: dict[str, dict[str, Any]] = {}
    for document in [*prior_history, *projected_history]:
        item = json.loads(json.dumps(dict(document)))
        observation_id = item["observation_id"]
        by_id.pop(observation_id, None)
        by_id[observation_id] = item
    history = list(by_id.values())[-64:]
    current = detached["activation_observation"]
    if not history or history[-1] != current:
        history.append(current)
        history = history[-64:]
    detached["activation_history"] = history
    detached["activation_observation"] = history[-1]
    return detached


class AffectedPeerQuarantined(RuntimeError):
    """Admission refused: a participating peer is liveness-quarantined.

    Raised before any node command is issued, so no command-ledger terminal
    CAS is owed — the router port publishes a bounded failed terminal
    directly instead of the cleanup-unproven nonterminal shape.
    """


class _ConcurrentNodeSession:
    """Adapt the command-demultiplexing process boundary to route evidence envelopes."""

    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        node_id: str,
        run_id: str,
        deployment_id: str,
        timeout_seconds: float,
    ) -> None:
        self._process = PhysicalNodeProcess(
            command=argv,
            node_id=node_id,
            run_id=run_id,
            deployment_id=deployment_id,
            response_timeout_seconds=timeout_seconds,
        )
        self.node_id = node_id

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def stderr(self) -> bytes:
        return self._process.stderr_tail.encode("utf-8", errors="replace")

    def send(
        self,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = self._process.command(
            command,
            payload,
            terminate_on_timeout=False,
            command_id=command_id,
        )
        return {
            "protocol": "mycelium.physical_node_control.v1",
            "command_id": command_id,
            "node_id": self.node_id,
            "ok": True,
            "route_ready": False,
            "result": result,
        }

    def send_before(
        self,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
        deadline_monotonic_s: float,
    ) -> dict[str, Any]:
        remaining = deadline_monotonic_s - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("command_deadline_exceeded")
        result = self._process.command(
            command,
            payload,
            timeout_seconds=remaining,
            terminate_on_timeout=False,
            command_id=command_id,
        )
        return {
            "protocol": "mycelium.physical_node_control.v1",
            "command_id": command_id,
            "node_id": self.node_id,
            "ok": True,
            "route_ready": False,
            "result": result,
        }

    def close(self) -> None:
        self._process.close()

    def interrupt_command(self, command_id: str, *, code: str) -> bool:
        return self._process.interrupt_command(command_id, code=code)


class TokenSink(Protocol):
    def emit(self, token_index: int, token_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class RouteCounters:
    frames_sent: int
    frames_received: int
    applied_operation_count: int
    fatal: str | None = None


@dataclass(frozen=True, slots=True)
class InferenceResult:
    request_id: str
    token_ids: tuple[int, ...]


class InferenceCancelled(RuntimeError):
    """The caller cancelled after admission and physical cleanup completed."""


def _request_is_cancelled(
    status: object,
    cancel_requested: Callable[[], bool] | None,
) -> bool:
    """Treat the physical terminal as authoritative across callback races."""

    return status == "CANCELLED" or (
        status == "DECODING" and cancel_requested is not None and cancel_requested()
    )


def _public_transport_health(
    health_observation: Mapping[str, Any] | object,
) -> dict[str, bool | str | None]:
    """Project bounded transport diagnostics without exposing error details."""

    details = (
        health_observation.get("details", {})
        if isinstance(health_observation, Mapping)
        else {}
    )
    if not isinstance(details, Mapping):
        details = {}
    sidecar = details.get("sidecar_process")
    fatal = details.get("transport_fatal_error")
    fatal_code: str | None = None
    if isinstance(fatal, Mapping):
        candidate = fatal.get("code")
        fatal_code = (
            candidate[:128]
            if isinstance(candidate, str) and candidate
            else "transport_failed"
        )
    return {
        "data_plane_health_observed": bool(health_observation),
        "sidecar_process_alive": (
            sidecar.get("alive")
            if isinstance(sidecar, Mapping) and type(sidecar.get("alive")) is bool
            else None
        ),
        "transport_running": (
            details.get("transport_running")
            if type(details.get("transport_running")) is bool
            else None
        ),
        "transport_fatal": isinstance(fatal, Mapping),
        "transport_fatal_code": fatal_code,
    }


# The per-peer counter fields of the public route status.  They are projected
# from a single signed observation, never assembled from fields sampled at
# different instants.
_PEER_COUNTER_FIELDS = (
    "frames_sent",
    "frames_received",
    "applied_operation_count",
    "prefill_operation_count",
    "decode_operation_count",
)

_PEER_OPERATION_TRIPLE = (
    "applied_operation_count",
    "prefill_operation_count",
    "decode_operation_count",
)


def _public_a10_publication(value: object) -> dict[str, Any] | None:
    """Project one node's A10 live publication, or None when absent or invalid."""

    if value is None:
        return None
    try:
        return _validate_a10_publication(value)
    except A10PublicationError:
        return None


def _observation_counter_material(
    observation: Mapping[str, Any] | object,
) -> dict[str, int] | None:
    """Read the counters carried by *one* signed observation, or None.

    The operation triple is reported only when this observation's own runtime
    block carries all three counters.  A partial view -- the lean health probe
    reports a lock-free ``applied`` counter and transport only, and a snapshot
    taken while a stage operation owns runtime state carries no counter block
    at all -- cannot substantiate the triple, and completing it from another
    observation is exactly the mixed provenance this projection exists to
    prevent.
    """

    details = (
        observation.get("details", {}) if isinstance(observation, Mapping) else {}
    )
    if not isinstance(details, Mapping):
        return None
    transport = details.get("transport", {})
    if not isinstance(transport, Mapping):
        transport = {}
    transport_counters = details.get("transport_counters", {})
    if not isinstance(transport_counters, Mapping):
        transport_counters = {}
    runtime = details.get("runtime", {})
    if not isinstance(runtime, Mapping):
        runtime = {}
    if not transport and not transport_counters and not runtime:
        return None
    material = {
        "frames_sent": int(
            transport.get(
                "remote_frames_sent",
                transport_counters.get("remote_frames_sent", 0),
            )
        ),
        "frames_received": int(
            transport.get(
                "remote_frames_received",
                transport_counters.get("remote_frames_received", 0),
            )
        ),
    }
    triple: dict[str, int] = {}
    for field in _PEER_OPERATION_TRIPLE:
        value = runtime.get(field)
        if type(value) is not int or value < 0:
            break
        triple[field] = value
    if len(triple) == len(_PEER_OPERATION_TRIPLE):
        material.update(triple)
    return material


def _peer_counter_projection(
    observation: Mapping[str, Any] | object,
    health_observation: Mapping[str, Any] | object,
) -> dict[str, int]:
    """Project one peer's counters from exactly ONE signed observation.

    Every executed operation does ``self._applied_operation_count += 1`` and
    increments exactly one of ``_prefill_operation_count`` /
    ``_decode_operation_count`` inside the same locked execution
    (``mycelium_router.mlx_runtime`` and its numpy twin), so one observed
    runtime block always satisfies ``applied == prefill + decode``.

    The publication used to mix provenances for that same triple: a per-field
    high-water ``max()`` across the full snapshot and the lean health probe
    supplied ``applied``, while ``prefill``/``decode`` came from the snapshot's
    runtime block alone.  A health probe sampling a later instant than the
    snapshot therefore emitted one instant's ``applied`` beside another
    instant's phases (live: ``applied 37`` beside ``prefill 9 + decode 3``),
    and the M23 ``one_token_decode`` gate correctly refused the record even
    though every runtime counter was healthy.

    If an atomic high-water is wanted, it has to be atomic: the whole
    observation carrying the greatest ``applied`` is published, its own
    ``prefill``/``decode`` and frames included.  A peer with no observation
    carrying the triple reports zeros rather than a blend of two.
    """

    material = [
        item
        for item in (
            _observation_counter_material(observation),
            _observation_counter_material(health_observation),
        )
        if item is not None
    ]
    complete = [item for item in material if "applied_operation_count" in item]
    if complete:
        # ``max`` keeps the first maximal item, so the snapshot observation
        # wins a tie with the health probe and the choice is deterministic.
        chosen = max(complete, key=lambda item: item["applied_operation_count"])
    elif material:
        chosen = material[0]
    else:
        chosen = {}
    return {field: int(chosen.get(field, 0)) for field in _PEER_COUNTER_FIELDS}


def _public_interruptibility(value: object) -> dict[str, Any] | None:
    """Reduce native replay telemetry without changing signed/private evidence.

    Source work bounds are not wall-time cleanup proof. Unavailable or invalid
    native telemetry stays unknown; the existing desktop variant is unchanged.
    """
    if not isinstance(value, Mapping):
        return None
    if value.get("reason") == "runtime_unavailable_or_not_cooperative":
        return None
    if value.get("work_unit") != "bounded_qwen2_replay_chunk":
        return None if value.get("runtime_backend") == "mobile-rust-qwen2" else dict(value)
    fields = (
        "runtime_backend", "decode_mode", "work_unit",
        "maximum_observed_work_unit_ms", "observed_work_unit_count",
        "maximum_total_cleanup_ms", "physical_proof_required",
        "backend_candidate", "cooperative_bound_candidate",
    )
    count = value.get("observed_work_unit_count")
    observed = value.get("maximum_observed_work_unit_ms")
    if (
        any(key not in value for key in fields)
        or value.get("runtime_backend") != "mobile-rust-qwen2"
        or value.get("decode_mode") != "complete_context_replay"
        or value.get("cooperative_interruption") is not True
        or value.get("request_scoped_cleanup") is not True
        or value.get("shared_process_termination_used") is not False
        or value.get("bound_kind") != "source_work_not_wall_time"
        or value.get("maximum_total_cleanup_ms") is not None
        or value.get("cooperative_bound_candidate") is not False
        or value.get("physical_proof_required") is not True
        or type(value.get("backend_candidate")) is not bool
        or type(count) is not int or not 0 <= count <= 2**53 - 1
        or (observed is not None and (
            not ((type(observed) is int and 0 <= observed <= 2**53 - 1)
                 or (type(observed) is float and math.isfinite(observed) and observed >= 0))
        ))
    ):
        return None
    return {key: value[key] for key in fields}


@dataclass(frozen=True, slots=True)
class RouteIdentity:
    deployment_id: str
    model_id: str
    resolved_commit: str
    endpoint_ids: tuple[str, ...]


class LiveSeedStateError(RuntimeError):
    """Stable fail-closed error while loading the live membership authority."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class LiveSeedAuthority:
    """Descriptor-bound, load-only view of one durable seed identity and database."""

    state_root: PrivateDirectoryLease
    signer: Ed25519EvidenceSigner
    database: Path
    swarm_id: str
    seed_node_id: str
    members: tuple[dict[str, Any], ...]
    authority_generation: int
    pseudonym_salt: bytes
    rotation_status: str | None
    rotation_observed_at: float | None

    def close(self) -> None:
        self.state_root.close()

    def current_members(self) -> tuple[dict[str, Any], ...]:
        self.state_root.revalidate()
        return tuple(
            {
                **member,
                "authority_generation": self.authority_generation,
                "rotation_status": self.rotation_status,
                "rotation_observed_at": self.rotation_observed_at,
            }
            for member in _read_live_seed_members(self.database)
        )

    def product_pseudonym_salt(self) -> bytes:
        """Derive one stable secret salt without exposing coordinator key bytes."""

        return self.pseudonym_salt


def _read_live_seed_members(database: Path) -> tuple[dict[str, Any], ...]:
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            member_rows = connection.execute(
                """
                SELECT node_id, endpoint_id, endpoint_addrs_json,
                       peer_class, runtime_capability_json,
                       verification_key_digest, incarnation, generation,
                       lease_expires_at, last_heartbeat_sequence,
                       last_liveness_at, next_heartbeat_due_at,
                       last_activity_receipt_at, active_requests,
                       lifecycle_state
                FROM seed_members
                ORDER BY node_id
                """
            ).fetchall()
        finally:
            connection.close()
        members = []
        for row in member_rows:
            member = SqliteSeedState._decode_member_row(row)
            # Activation eligibility is a derived policy decision, not durable
            # member-controlled state.  Keep the database schema free of that
            # claim while ensuring every live consumer evaluates the exact same
            # peer-class/runtime-capability rule used during route construction.
            member["activation_eligible"] = peer_runtime_is_activation_eligible(
                member["peer_class"],
                member["runtime_capability"],
            )
            members.append(member)
        return tuple(members)
    except (SeedStateError, sqlite3.Error) as exc:
        raise LiveSeedStateError("live_seed_database_corrupt") from exc


def _load_live_seed_authority(
    *,
    seed_state_root: Path,
    plan_membership_snapshot: Mapping[str, Any],
) -> LiveSeedAuthority:
    """Load and bind an existing seed root without creating or rewriting it."""

    lease: PrivateDirectoryLease | None = None
    try:
        lease = private_directory_lease(seed_state_root, create=False)
        if not lease.exists:
            raise LiveSeedStateError("live_seed_state_missing")
        lease.revalidate()
        database = lease.path / "state.sqlite3"
        try:
            metadata = database.lstat()
        except FileNotFoundError as exc:
            raise LiveSeedStateError("live_seed_database_missing") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise LiveSeedStateError("live_seed_database_path_invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise LiveSeedStateError("live_seed_database_permissions_invalid")

        uri = f"file:{quote(str(database), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only = ON")
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                if quick_check is None or quick_check[0] != "ok":
                    raise LiveSeedStateError("live_seed_database_corrupt")
                rows = connection.execute(
                    "SELECT key, value FROM seed_metadata "
                    "WHERE key IN ('swarm_id', 'seed_node_id', 'seed_key_digest', "
                    "'authority_generation')"
                ).fetchall()
                rotation_row = connection.execute(
                    "SELECT status, initiated_at FROM seed_authority_rotations "
                    "ORDER BY authority_generation DESC LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
        except LiveSeedStateError:
            raise
        except sqlite3.Error as exc:
            raise LiveSeedStateError("live_seed_database_corrupt") from exc
        binding = {
            row["key"]: row["value"]
            for row in rows
            if isinstance(row["key"], str) and isinstance(row["value"], str)
        }
        if set(binding) != {
            "swarm_id",
            "seed_node_id",
            "seed_key_digest",
            "authority_generation",
        }:
            raise LiveSeedStateError("live_seed_database_identity_missing")
        try:
            authority_generation = int(binding["authority_generation"])
        except ValueError as exc:
            raise LiveSeedStateError("live_seed_database_identity_missing") from exc
        if (
            authority_generation < 1
            or str(authority_generation) != binding["authority_generation"]
        ):
            raise LiveSeedStateError("live_seed_database_identity_missing")
        try:
            signer = load_bound_seed_signer(
                lease.path / "identity",
                expected_digest=binding["seed_key_digest"],
            )
        except SeedAuthorityError as exc:
            mapping = {
                "seed_authority_key_missing": "live_seed_identity_missing",
                "node_identity_permissions_invalid": (
                    "live_seed_identity_permissions_invalid"
                ),
                "node_identity_invalid": "live_seed_identity_invalid",
                "seed_authority_key_mismatch": "live_seed_database_identity_mismatch",
            }
            raise LiveSeedStateError(
                mapping.get(exc.code, "live_seed_identity_path_invalid")
            ) from exc
        pseudonym_path = lease.path / "identity" / PRODUCT_PSEUDONYM_KEY_FILE
        if pseudonym_path.exists() or pseudonym_path.is_symlink():
            try:
                pseudonym_salt = load_product_pseudonym_salt(lease.path / "identity")
            except SeedAuthorityError as exc:
                raise LiveSeedStateError("live_seed_product_identity_invalid") from exc
        elif authority_generation == 1:
            pseudonym_salt = derive_product_pseudonym_salt(
                signer,
                swarm_id=binding["swarm_id"],
            )
        else:
            raise LiveSeedStateError("live_seed_product_identity_missing")
        plan_digest = plan_membership_snapshot.get("seed_key_digest")
        if plan_digest != signer.verification_key_digest:
            raise LiveSeedStateError("live_seed_plan_identity_mismatch")
        plan_swarm_id = plan_membership_snapshot.get("swarm_id")
        if plan_swarm_id != binding["swarm_id"]:
            raise LiveSeedStateError("live_seed_plan_swarm_mismatch")
        members = _read_live_seed_members(database)
        rotation_status = None
        rotation_observed_at = None
        if rotation_row is not None:
            rotation_status = str(rotation_row["status"]).lower()
            rotation_observed_at = float(rotation_row["initiated_at"])
        lease.revalidate()
        return LiveSeedAuthority(
            state_root=lease,
            signer=signer,
            database=database,
            swarm_id=binding["swarm_id"],
            seed_node_id=binding["seed_node_id"],
            members=members,
            authority_generation=authority_generation,
            pseudonym_salt=pseudonym_salt,
            rotation_status=rotation_status,
            rotation_observed_at=rotation_observed_at,
        )
    except LiveSeedStateError:
        if lease is not None:
            lease.close()
        raise
    except (OSError, ValueError) as exc:
        if lease is not None:
            lease.close()
        raise LiveSeedStateError("live_seed_state_invalid") from exc


class LiveRoute(Protocol):
    def open(self) -> RouteIdentity: ...

    def infer(
        self,
        token_ids: Sequence[int],
        *,
        max_new_tokens: int,
        request_id: str,
        sink: TokenSink,
        cancel_requested: Callable[[], bool] | None = None,
        selected_placement_ids: Sequence[str] | None = None,
        route_identity: Mapping[str, Any] | None = None,
        locked_path_manifest: Mapping[str, Any] | None = None,
        command_identity: Mapping[str, Any] | None = None,
        authorize_cleanup: Callable[[float], float] | None = None,
        publish_cleanup_receipt: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> InferenceResult: ...

    def release_request(self, request_id: str) -> None: ...

    def counters(self) -> RouteCounters: ...

    def public_status(self) -> Mapping[str, Any]: ...

    def is_alive(self) -> bool: ...

    def close(self) -> None: ...

    def cleanup(self) -> None: ...


class FakeLiveRoute:
    """Deterministic test double. Never valid as a product path."""

    is_simulated = True

    def __init__(self, *, scripted_tokens: Sequence[int]) -> None:
        self._scripted = tuple(scripted_tokens)
        self._open = False
        self._closed = False
        self._frames_sent = 0
        self._frames_received = 0
        self._operations = 0
        self._recent_inferences: deque[dict[str, Any]] = deque(maxlen=64)
        self._incidents: deque[dict[str, Any]] = deque(maxlen=64)
        self._placement_projection: dict[str, Any] | None = None
        self._topology_projection: dict[str, Any] | None = None
        self._model_operation: dict[str, Any] | None = None
        self._m16_runtime_source: Callable[[], Mapping[str, Any] | None] | None = None

    def open(self) -> RouteIdentity:
        if self._closed:
            raise RuntimeError("route_closed")
        self._open = True
        return RouteIdentity(
            deployment_id="12345678-1234-5678-9234-abcdefabcdef",
            model_id="microsoft/DialoGPT-small",
            resolved_commit="49c537161a457d5256512f9d2d38a87d81ae0f0e",
            endpoint_ids=("fake-endpoint-0", "fake-endpoint-1"),
        )

    def infer(
        self,
        token_ids: Sequence[int],
        *,
        max_new_tokens: int,
        request_id: str,
        sink: TokenSink,
        cancel_requested: Callable[[], bool] | None = None,
        selected_placement_ids: Sequence[str] | None = None,
        route_identity: Mapping[str, Any] | None = None,
        locked_path_manifest: Mapping[str, Any] | None = None,
        command_identity: Mapping[str, Any] | None = None,
        authorize_cleanup: Callable[[float], float] | None = None,
        publish_cleanup_receipt: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> InferenceResult:
        if not self.is_alive():
            raise RuntimeError("route_not_open")
        before = self.counters()
        started_at = time.monotonic()
        emitted = self._scripted[:max_new_tokens]
        for index, token_id in enumerate(emitted):
            if cancel_requested is not None and cancel_requested():
                raise InferenceCancelled("inference_cancelled")
            self._frames_sent += 1
            self._frames_received += 1
            self._operations += 1
            sink.emit(index, token_id)
        completed_at = time.monotonic()
        after = self.counters()
        self._recent_inferences.append(
            {
                "context_tokens": len(token_ids),
                "output_tokens": len(emitted),
                "prefill_ms": 0.0,
                "ttft_ms": 0.0 if emitted else None,
                "tpot_ms": 0.0 if len(emitted) > 1 else None,
                "total_ms": (completed_at - started_at) * 1_000.0,
                "peer_counter_deltas": [
                    {
                        "node_id": "fake-node",
                        "frames_sent": after.frames_sent - before.frames_sent,
                        "frames_received": after.frames_received
                        - before.frames_received,
                        "applied_operation_count": (
                            after.applied_operation_count
                            - before.applied_operation_count
                        ),
                    }
                ],
            }
        )
        return InferenceResult(request_id=request_id, token_ids=emitted)

    def release_request(self, request_id: str) -> None:
        return

    def counters(self) -> RouteCounters:
        return RouteCounters(
            frames_sent=self._frames_sent,
            frames_received=self._frames_received,
            applied_operation_count=self._operations,
        )

    def public_status(self) -> Mapping[str, Any]:
        return {
            "protocol": "mycelium.live_route_status.v1",
            "route_alive": self.is_alive(),
            "simulated": True,
            "route_identity_digest": None,
            "deployment_id": "12345678-1234-5678-9234-abcdefabcdef",
            "model_id": "microsoft/DialoGPT-small",
            "topology_version": 1,
            "decode_mode": "complete_context_replay",
            "counters": {
                "frames_sent": self._frames_sent,
                "frames_received": self._frames_received,
                "applied_operation_count": self._operations,
                "fatal": None,
            },
            "stages": [],
            "peers": [],
            "recent_inferences": list(self._recent_inferences),
            "incidents": list(self._incidents),
        }

    def membership_status(self, *, qualification: Any | None) -> Mapping[str, Any]:
        state = "qualified" if qualification is not None else "assigned"
        return {
            "protocol": "mycelium.product_ui.swarm.v1",
            "native_nodes": [
                {
                    "member_id": node_id,
                    "capability": "native_inference_node",
                    "membership_state": state,
                    "connectivity": "unknown",
                    "endpoint_id": None,
                }
                for node_id in ("node-a", "node-b")
            ],
            "browser_workers": [],
        }

    def product_membership_records(self) -> tuple[dict[str, Any], ...]:
        now = time.time()
        return tuple(
            {
                "node_id": node_id,
                "peer_class": "mac_mlx_iroh",
                "runtime_capability": {
                    "runtime_backend": "mlx",
                    "transport": "iroh",
                    "activation_protocol": "mycelium.router_wire.v1",
                },
                "activation_eligible": True,
                "generation": 1,
                "incarnation": "fixture-incarnation",
                "lease_expires_at": now + 300.0,
                "last_liveness_at": now,
                "lifecycle_state": "RUNNING",
            }
            for node_id in ("node-a", "node-b")
        )

    @staticmethod
    def product_pseudonym_salt() -> bytes:
        return hashlib.sha256(b"mycelium-fake-route-product-salt").digest()

    def is_alive(self) -> bool:
        return self._open and not self._closed

    def close(self) -> None:
        self._open = False
        self._closed = True

    def cleanup(self) -> None:
        return


def _refresh_membership_snapshot(
    snapshot: Mapping[str, Any],
    *,
    now: float,
    signer: Ed25519EvidenceSigner,
    seed_node_id: str,
    members: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Reissue assignment offers from current coordinator membership facts."""

    offers = snapshot.get("assignment_offers")
    if not isinstance(offers, list) or not offers:
        raise ValueError("membership_snapshot_invalid")
    member_by_node: dict[str, Mapping[str, Any]] = {}
    for member in members:
        node_id = member.get("node_id") if isinstance(member, Mapping) else None
        if not isinstance(node_id, str) or not node_id or node_id in member_by_node:
            raise ValueError("membership_member_invalid")
        member_by_node[node_id] = member
    recipients = []
    activation_endpoint_by_node: dict[str, str] = {}
    for envelope in offers:
        message = envelope.get("message") if isinstance(envelope, Mapping) else None
        recipient = (
            message.get("recipient_node_id") if isinstance(message, Mapping) else None
        )
        if not isinstance(recipient, str) or not recipient or recipient in recipients:
            raise ValueError("membership_snapshot_invalid")
        recipients.append(recipient)
        records = message.get("peer_endpoint_records")
        if not isinstance(records, list):
            raise ValueError("membership_snapshot_invalid")
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("membership_snapshot_invalid")
            node_id = record.get("node_id")
            endpoint_id = record.get("endpoint_id")
            if (
                not isinstance(node_id, str)
                or not node_id
                or not isinstance(endpoint_id, str)
                or not endpoint_id
            ):
                raise ValueError("membership_activation_endpoint_invalid")
            prior = activation_endpoint_by_node.setdefault(node_id, endpoint_id)
            if prior != endpoint_id:
                raise ValueError("membership_activation_endpoint_conflict")
    route_members: dict[str, Mapping[str, Any]] = {}
    for recipient in recipients:
        member = member_by_node.get(recipient)
        if member is None:
            raise ValueError("membership_member_missing")
        try:
            generation = member["generation"]
            lease_expires_at = float(member["lease_expires_at"])
            endpoint_id = member["endpoint_id"]
            capable = peer_runtime_is_activation_eligible(
                member["peer_class"],
                member["runtime_capability"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("membership_member_invalid") from exc
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or not isinstance(endpoint_id, str)
            or not endpoint_id
        ):
            raise ValueError("membership_member_invalid")
        if lease_expires_at <= now:
            raise ValueError("membership_member_lease_expired")
        if not capable:
            raise ValueError("membership_member_activation_ineligible")
        route_members[recipient] = member
    if len(route_members) > 1 and set(activation_endpoint_by_node) != set(
        route_members
    ):
        raise ValueError("membership_activation_endpoint_missing")
    valid_until = min(
        now + 3_600.0,
        *(float(member["lease_expires_at"]) for member in route_members.values()),
    )
    refreshed_offers = []
    for index, envelope in enumerate(offers):
        if not isinstance(envelope, Mapping) or not isinstance(
            envelope.get("message"), Mapping
        ):
            raise ValueError("membership_snapshot_invalid")
        message = json.loads(json.dumps(envelope["message"]))
        recipient = message["recipient_node_id"]
        member = route_members[recipient]
        message["message_id"] = f"live-route-offer-{index}-{int(now * 1_000)}"
        message["sender_node_id"] = seed_node_id
        message["sender_endpoint_id"] = signer.endpoint_id
        message["incarnation"] = f"live-seed-{int(now * 1_000)}"
        message["generation"] = member["generation"]
        message["issued_at"] = now
        message["expires_at"] = valid_until
        message["peer_endpoint_records"] = [
            {
                "node_id": node_id,
                # Membership endpoint identity authenticates the current member and
                # generation. It is not the planner-authorized activation-plane Iroh
                # identity. Configure separately proves possession of that key.
                "endpoint_id": activation_endpoint_by_node[node_id],
                "deployment_epoch": message["deployment_epoch"],
                "membership_generation": other["generation"],
                "valid_from": now,
                "valid_until": valid_until,
            }
            for node_id, other in sorted(route_members.items())
            if node_id != recipient
        ]
        refreshed_offers.append(sign_membership_message(signer=signer, message=message))
    return {
        **dict(snapshot),
        "seed_key_digest": signer.verification_key_digest,
        "assignment_offers": refreshed_offers,
    }


_TEST_FAULT_IDENTITY_FIELDS = (
    "deployment_id", "deployment_epoch", "qualification_digest", "request_id",
    "request_attempt", "path_id", "path_attempt", "path_digest",
    "topology_generation", "command_id", "publisher_generation",
    "cancellation_generation",
)


@dataclass(frozen=True, slots=True)
class _TestOnlyTransportFault:
    """Private, exact one-command fault input; never derived from HTTP or a global flag.

    The caller must explicitly inject this into a route constructor. The route
    only consumes it on its assigned, bound inference source command. A local
    fixture is not evidence of physical failure or gateway admission.
    """

    identity: Mapping[str, Any]
    source_node_id: str
    command: str
    deadline_monotonic_ms: int
    transport_command_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identity, Mapping)
            or set(self.identity) != set(_TEST_FAULT_IDENTITY_FIELDS)
            or any(type(self.identity[field]) is not int or self.identity[field] < 0
                   for field in ("deployment_epoch", "request_attempt", "path_attempt",
                                 "topology_generation", "publisher_generation",
                                 "cancellation_generation"))
            or any(not isinstance(self.identity[field], str) or not self.identity[field]
                   for field in ("deployment_id", "qualification_digest", "request_id",
                                 "path_id", "path_digest", "command_id"))
            or self.identity["cancellation_generation"] != 0
            or not isinstance(self.source_node_id, str) or not self.source_node_id
            or self.command not in ("infer_start", "infer_decode")
            or type(self.deadline_monotonic_ms) is not int
            or self.deadline_monotonic_ms < 1
            or self.deadline_monotonic_ms > int(time.monotonic() * 1_000) + 60_000
            or (self.transport_command_id is not None and
                (not isinstance(self.transport_command_id, str) or not self.transport_command_id))
        ):
            raise ValueError("invalid_test_transport_fault")
        object.__setattr__(self, "identity", MappingProxyType(dict(self.identity)))

class _TestOnlyTransportFaultSlot:
    """Constructor-only opt-in; populated solely while the route lock is held.

    Never passed through an HTTP constructor or production operator plan.
    """

    def __init__(self) -> None:
        self.fault: _TestOnlyTransportFault | None = None
        self.disabled = False


_OPEN_ROUTES: "weakref.WeakSet[Any]" = weakref.WeakSet()
_OPEN_ROUTES_LOCK = threading.Lock()


def _register_open_route(route: Any) -> None:
    with _OPEN_ROUTES_LOCK:
        _OPEN_ROUTES.add(route)


def _unregister_open_route(route: Any) -> None:
    with _OPEN_ROUTES_LOCK:
        _OPEN_ROUTES.discard(route)


def open_route_peer_liveness() -> "dict[str, dict[str, Any]] | None":
    """Per-peer liveness of the route open in this process (``/api/status`` -> ``peer_liveness``), else ``None``."""

    with _OPEN_ROUTES_LOCK:
        routes = [route for route in _OPEN_ROUTES if getattr(route, "_open", False) and not getattr(route, "_closed", True)]
    if not routes:
        return None
    try:
        return routes[-1].peer_liveness_overview()
    except Exception:
        return None


class PhysicalLiveRoute:
    """Persistent multi-host route composed from the physical node protocol."""

    is_simulated = False

    def __init__(
        self,
        *,
        controller: QualificationController,
        endpoints: Mapping[str, Mapping[str, Any]],
        run_plan: Mapping[str, Any],
        session_factory=_ConcurrentNodeSession,
        external_session_factory=None,
        seed_authority: LiveSeedAuthority | None = None,
        membership_snapshot: Mapping[str, Any] | None = None,
        force_relay: bool = False,
        _test_only_transport_fault: _TestOnlyTransportFault | _TestOnlyTransportFaultSlot | None = None,
    ) -> None:
        if not isinstance(force_relay, bool):
            raise ValueError("force_relay must be a boolean")
        if _test_only_transport_fault is not None and not isinstance(
            _test_only_transport_fault, (_TestOnlyTransportFault, _TestOnlyTransportFaultSlot)
        ):
            raise ValueError("invalid_test_transport_fault")
        self._controller = controller
        self._peers = {peer.node_id: peer for peer in controller.peers}
        self._endpoints = {
            node_id: dict(endpoint) for node_id, endpoint in endpoints.items()
        }
        self._plan = dict(run_plan)
        self._plans_by_node = {
            item["node_id"]: dict(item) for item in self._plan["nodes"]
        }
        self._graph = execution_graph_from_document(
            self._plan["nodes"][0]["configure"]["graph"]
        )
        self._session_factory = session_factory
        self._external_session_factory = external_session_factory
        self._seed_authority = seed_authority
        self._membership_snapshot = (
            None
            if membership_snapshot is None
            else json.loads(json.dumps(dict(membership_snapshot)))
        )
        self._force_relay = force_relay
        self._sessions: dict[str, Any] = {}
        self._identities: dict[str, dict[str, Any]] = {}
        self._verification_keys: dict[str, dict[str, Any]] = {}
        self._endpoint_addresses: dict[str, dict[str, Any]] = {}
        self._signed_observations: list[dict[str, Any]] = []
        self._last_snapshots: dict[str, dict[str, Any]] = {}
        self._cleanup_snapshots_by_request: dict[str, dict[str, dict[str, Any]]] = {}
        self._last_health: dict[str, dict[str, Any]] = {}
        self._request_inputs: dict[str, tuple[int, ...]] = {}
        self._request_outputs: dict[str, tuple[int, ...]] = {}
        self._request_limits: dict[str, int] = {}
        self._request_entry_nodes: dict[str, str] = {}
        self._recent_inferences: deque[dict[str, Any]] = deque(maxlen=64)
        self._product_activation_history: deque[dict[str, Any]] = deque(maxlen=64)
        self._incidents: deque[dict[str, Any]] = deque(maxlen=64)
        self._incident_sequence = 0
        self._stop_token_ids: frozenset[int] = frozenset()
        # Live measurement state.  ``_hop_recorder`` is disabled unless
        # MYCELIUM_ROUTER_INSTRUMENT (plus MYCELIUM_ROUTER_INSTRUMENT_SINK) is
        # set, so a real run opts in from the environment and emits hop traces
        # with no test-side wrapping.  ``_measured_rtt`` is where every round
        # trip this route performs is filed, for the route cost model to use.
        self._hop_recorder = HopRecorder.from_environment()
        self._measured_rtt = MeasuredRttTable()
        self._active_hop_traces: dict[str, Any] = {}
        self._placement_projection: dict[str, Any] | None = None
        self._topology_projection: dict[str, Any] | None = None
        self._workload_comparison: dict[str, Any] | None = None
        self._model_operation: dict[str, Any] | None = None
        self._deployment_qualification: Any | None = None
        self._deployment_quantization: str | None = None
        self._command_sequence = 0
        self._open = False
        self._closed = False
        # Latched lock-free by abort_open(); open() checks it between nodes.
        self._open_aborted = False
        self._fatal: str | None = None
        self._m16_runtime_source: Callable[[], Mapping[str, Any] | None] | None = None
        self._lock = threading.RLock()
        self._request_locks: dict[str, threading.RLock] = {}
        self._active_route_requests: dict[str, dict[str, Any]] = {}
        self._test_only_transport_fault = _test_only_transport_fault
        self._test_only_transport_fault_consumed = False
        self._pending_publisher_generations: dict[
            str, tuple[int, int, dict[str, Any]]
        ] = {}
        self._request_cleanup_receipts: dict[str, dict[str, Any]] = {}
        self._cleanup_reconciliation_stop = threading.Event()
        self._cleanup_reconciliation_executor = ThreadPoolExecutor(
            max_workers=_CLEANUP_RECONCILIATION_WORKERS,
            thread_name_prefix="mycelium-cleanup-reconcile",
        )
        self._cleanup_reconciliation_futures: dict[str, Any] = {}
        self._response_reader_failure_incidents: dict[tuple[str, str, str], None] = {}
        self._scoped_runtime_incidents: deque[dict[str, Any]] = deque(maxlen=64)
        self._scoped_runtime_incident_sequence = 0
        self._a4_qualification: dict[str, Any] | None = None
        self._replica_track_qualifications: tuple[dict[str, Any], ...] = ()
        self._replica_loss_placement_ids: frozenset[str] = frozenset()
        self._liveness = TrafficAwareLivenessDetector()
        self._liveness_subjects: dict[str, LivenessSubject] = {}
        self._liveness_edge_subjects: dict[str, LivenessSubject] = {}
        self._last_transport_event_sequence: dict[str, int] = {}
        self._liveness_monitor_stop = threading.Event()
        self._liveness_monitor_thread: threading.Thread | None = None

    def _record_incident(
        self,
        *,
        state: str,
        reason: str,
        request_id: str | None,
    ) -> None:
        with self._lock:
            last = self._incidents[-1] if self._incidents else None
            if (
                last is not None
                and last["state"] == state
                and last["reason"] == reason
                and last["request_id"] == request_id
            ):
                return
            self._incident_sequence += 1
            self._incidents.append(
                {
                    "protocol": "mycelium.live_route_incident.v1",
                    "incident_id": f"route-incident-{self._incident_sequence}",
                    "deployment_id": self._plan["deployment_id"],
                    "request_id": request_id,
                    "state": state,
                    "reason": reason[:128],
                    "observed_at_unix_ms": int(time.time() * 1_000),
                }
            )

    def _record_scoped_runtime_incident(
        self,
        *,
        request_id: str,
        subject_id: str,
        scope: str,
        reason: str,
        fatal_requested: bool = False,
        fatal_accepted: bool = False,
    ) -> None:
        """Record one privacy-reduced incident against the exact active identity."""

        from mycelium_live.a4_contracts import validate_scoped_runtime_incident

        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None:
                return
            self._scoped_runtime_incident_sequence += 1
            document = validate_scoped_runtime_incident(
                {
                    "protocol": "mycelium.scoped_runtime_incident.v1",
                    "incident_id": (
                        f"scoped-runtime-{self._scoped_runtime_incident_sequence}"
                    ),
                    "deployment_id": active["deployment_id"],
                    "deployment_epoch": active["deployment_epoch"],
                    "qualification_digest": active["qualification_digest"],
                    "request_id": request_id,
                    "request_attempt": active["request_attempt"],
                    "path_id": active["path_id"],
                    "path_attempt": active["path_attempt"],
                    "path_digest": active["path_digest"],
                    "topology_generation": active["topology_generation"],
                    "command_id": active["command_id"],
                    "cancellation_generation": active["cancellation_generation"],
                    "publisher_generation": active["publisher_generation"],
                    "cleanup_owner_id": (
                        f"physical-live-route:{active['deployment_id']}"
                    ),
                    "subject_id": subject_id,
                    "scope": scope,
                    "reason": reason[:128],
                    "fatal_requested": fatal_requested,
                    "fatal_accepted": fatal_accepted,
                    "observed_at_monotonic_ms": int(time.monotonic() * 1_000),
                }
            )
            self._scoped_runtime_incidents.append(document)

    def a4_scoped_runtime_incidents(self) -> tuple[Mapping[str, Any], ...]:
        """Return detached, bounded A4 incident evidence without prompt material."""

        with self._lock:
            return tuple(
                json.loads(json.dumps(item)) for item in self._scoped_runtime_incidents
            )

    def _install_test_only_transport_fault(self, fault: _TestOnlyTransportFault) -> bool:
        """Fail closed under the route lock; the command rechecks at consumption."""
        with self._lock:
            slot = self._test_only_transport_fault
            active = self._active_route_requests.get(fault.identity["request_id"])
            if (
                not isinstance(slot, _TestOnlyTransportFaultSlot)
                or not isinstance(fault, _TestOnlyTransportFault)
                or slot.fault is not None or slot.disabled
                or self._test_only_transport_fault_consumed
                or self._closed or not self._open
                or active is None or active.get("terminal") is not False
                or active.get("control_bound") is not True
                or active.get("cancellation_requested") is not False
                or active.get("entry_node_id") != fault.source_node_id
                or active.get("cancellation_generation") != 0
                or fault.identity["request_id"] in self._request_cleanup_receipts
                or int(time.monotonic() * 1000) >= min(
                    fault.deadline_monotonic_ms, active.get("absolute_deadline_ms", 0))
                or any(active.get(field) != fault.identity[field]
                       for field in _TEST_FAULT_IDENTITY_FIELDS if field != "request_id")
            ):
                return False
            slot.fault = fault
            return True

    def _record_active_runtime_failure(
        self,
        *,
        request_id: str,
        node_id: str,
        reason: str,
        observed_at_monotonic_s: float,
    ) -> None:
        """Route a verified command failure through scoped liveness, never fatal."""

        subject = self._liveness_subjects.get(node_id)
        if subject is None:
            return
        observed_at_ms = int(observed_at_monotonic_s * 1_000)
        transport_failure = reason in {
            "ack_failed",
            "delivery_deadline_exceeded",
            "delivery_not_confirmed",
            "delivery_cancelled",
            "path_cancelled",
            "peer_rotated",
            "prefill_transport_stalled",
            "sidecar_queue_full",
            "transport_not_running",
        } or reason.startswith(("sidecar_", "transport_"))
        if transport_failure:
            self._liveness.record_active_failure(
                subject,
                failure_started_at_ms=observed_at_ms,
                observed_at_ms=observed_at_ms,
                scope="request",
                affected_track_ids=(request_id,),
                verified=True,
            )
        else:
            self._liveness.record_worker_exception(
                subject,
                request_id=request_id,
                observed_at_ms=observed_at_ms,
            )
        self._record_scoped_runtime_incident(
            request_id=request_id,
            subject_id=node_id,
            scope="request",
            reason=reason,
        )

    def _record_response_reader_failure_once(
        self,
        *,
        request_id: str,
        node_id: str,
        reason: str,
        observed_at_monotonic_s: float,
    ) -> bool:
        """Project one fatal response-reader code onto its exact active request."""

        if reason not in _NODE_RESPONSE_READER_FAILURE_CODES:
            return False
        key = (request_id, node_id, reason)
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None:
                return True
            recorded = getattr(self, "_response_reader_failure_incidents", None)
            if recorded is None:
                recorded = {}
                self._response_reader_failure_incidents = recorded
            if key in recorded:
                return True
            recorded[key] = None
            while len(recorded) > 4_096:
                recorded.pop(next(iter(recorded)))
        self._record_active_runtime_failure(
            request_id=request_id,
            node_id=node_id,
            reason=reason,
            observed_at_monotonic_s=observed_at_monotonic_s,
        )
        return True

    @classmethod
    def from_operator_plan(
        cls,
        plan_path: Path,
        *,
        seed_state_root: Path,
        force_relay: bool = False,
        session_factory=None,
        external_session_factory=None,
    ) -> "PhysicalLiveRoute":
        document = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        controller_document = document["controller"]
        authority = _load_live_seed_authority(
            seed_state_root=seed_state_root,
            plan_membership_snapshot=controller_document["membership_snapshot"],
        )
        now = time.time()
        try:
            membership_snapshot = _refresh_membership_snapshot(
                controller_document["membership_snapshot"],
                now=now,
                signer=authority.signer,
                seed_node_id=authority.seed_node_id,
                members=authority.members,
            )
            peers = tuple(PeerIdentity(**item) for item in controller_document["peers"])
            if external_session_factory is None and any(p.get("process_transport") == "external_iroh" for p in controller_document["peers"]):
                from .phone_stage_session import PhoneStageSessionFactory
                external_session_factory = PhoneStageSessionFactory.from_descriptor(
                    controller_document.get("external_control")
                )
            controller = QualificationController(
                mode=controller_document["mode"],
                peers=peers,
                source_root=Path(controller_document["source_root"]),
                transfer_manifest=controller_document["transfer_manifest"],
                node_transfer_manifests=controller_document.get(
                    "node_transfer_manifests"
                ),
                prepositioned_artifacts=controller_document.get(
                    "prepositioned_artifacts"
                ),
                membership_snapshot=membership_snapshot,
                now=now,
                run_plan=controller_document["run_plan"],
                session_factory=session_factory,
            )
            controller._validate_physical_distinctness()
            endpoints = controller._validate_membership()
            run_plan = controller._validate_run_plan()
            return cls(
                controller=controller,
                endpoints=endpoints,
                run_plan=run_plan,
                seed_authority=authority,
                membership_snapshot=membership_snapshot,
                force_relay=force_relay,
                session_factory=session_factory or _ConcurrentNodeSession,
                external_session_factory=external_session_factory,
            )
        except BaseException:
            authority.close()
            raise

    def _command_id(self, node_id: str, operation: str) -> str:
        with self._lock:
            self._command_sequence += 1
            # Durable managed mailboxes outlive a route. Preserve deterministic
            # retries within one run without colliding with a later deployment.
            identity = [self._plan["run_id"], self._plan["deployment_id"],
                        node_id, operation, self._command_sequence]
            encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return "cmd-" + hashlib.sha256(encoded).hexdigest()

    def _send_request_command(
        self,
        *,
        request_id: str,
        node_id: str,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None or active.get("terminal") is True:
                raise RuntimeError("request_control_identity_unavailable")
            inflight = active.setdefault("inflight_commands", {})
            if node_id in inflight:
                raise RuntimeError("request_node_command_already_inflight")
            inflight[node_id] = command_id
        try:
            try:
                with self._lock:
                    active = self._active_route_requests.get(request_id)
                    configured = getattr(self, "_test_only_transport_fault", None)
                    fault = (None if configured.disabled else configured.fault) if isinstance(
                        configured, _TestOnlyTransportFaultSlot
                    ) else configured
                    qualification_matches = False
                    if fault is not None and active is not None:
                        qualification = self._deployment_qualification
                        if qualification is not None and getattr(qualification, "route_ready", False) is True:
                            from mycelium_request_gateway.contracts import qualification_digest

                            try:
                                qualification_matches = (
                                    qualification_digest(qualification)
                                    == active.get("qualification_digest")
                                )
                            except (TypeError, ValueError, KeyError, AttributeError):
                                # A malformed or revoked authority cannot make
                                # a test fault eligible or mask the real send.
                                pass
                    if (
                        fault is not None
                        and not self._test_only_transport_fault_consumed
                        and qualification_matches
                        and active is not None
                        and not self._closed
                        and active.get("terminal") is False
                        and active.get("control_bound") is True
                        and active.get("cancellation_requested") is False
                        and active.get("cancellation_generation") == 0
                        and request_id not in self._request_cleanup_receipts
                        and active.get("entry_node_id") == node_id == fault.source_node_id
                        and node_id in active.get("participating_node_ids", ())
                        and command == fault.command
                        and (fault.transport_command_id is None or
                             command_id == fault.transport_command_id)
                        and active.get("inflight_commands", {}).get(node_id) == command_id
                        and int(time.monotonic() * 1_000) < min(
                            fault.deadline_monotonic_ms,
                            active.get("absolute_deadline_ms", 0),
                        )
                        and fault.identity["request_id"] == request_id
                        and all(active.get(field) == fault.identity[field]
                                for field in _TEST_FAULT_IDENTITY_FIELDS
                                if field != "request_id")
                        and (payload.get("request_id") == request_id or
                             (command == "infer_start" and
                              isinstance(payload.get("request"), Mapping) and
                              payload["request"].get("request_id") == request_id))
                        and isinstance(payload.get("control"), Mapping)
                        and all(payload["control"].get(field) == active[field]
                                for field in _TEST_FAULT_IDENTITY_FIELDS
                                if field != "request_id")
                    ):
                        # Linearize once before the real session.send. Do not
                        # intercept its return/error, or write A4 state here.
                        self._test_only_transport_fault_consumed = True
                        raise RuntimeError("transport_not_running")
                # TRUE live-boundary spans: stamp immediately before the command
                # write and immediately after the correlated response arrives.  A
                # failed or timed-out send is not a latency sample, so nothing is
                # filed on the error path — the span is only recorded when the
                # round trip actually completed.
                send_started_at = time.monotonic()
                response = self._sessions[node_id].send(
                    command_id=command_id,
                    command=command,
                    payload=payload,
                )
                self._record_measured_round_trip(
                    node_id,
                    started_at=send_started_at,
                    finished_at=time.monotonic(),
                    stage_id=command,
                    request_id=request_id,
                )
                return response
            except BaseException as error:
                reason = str(getattr(error, "code", str(error) or type(error).__name__))
                self._record_response_reader_failure_once(
                    request_id=request_id,
                    node_id=node_id,
                    reason=reason,
                    observed_at_monotonic_s=time.monotonic(),
                )
                raise
        finally:
            with self._lock:
                active = self._active_route_requests.get(request_id)
                if active is not None:
                    inflight = active.get("inflight_commands")
                    if (
                        isinstance(inflight, dict)
                        and inflight.get(node_id) == command_id
                    ):
                        inflight.pop(node_id, None)

    def _request_execution_lock(self, request_id: str) -> threading.RLock:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("invalid_inference_request")
        with self._lock:
            locks = getattr(self, "_request_locks", None)
            if locks is None:
                locks = {}
                self._request_locks = locks
            return locks.setdefault(request_id, threading.RLock())

    # -- live measurement -------------------------------------------------

    @contextmanager
    def _instrumented_request(self, request_id: str) -> Iterator[None]:
        """Hold the request execution lock and open one hop trace for it.

        Identical locking semantics to ``_request_execution_lock`` alone: the
        same request-scoped RLock is acquired for the whole ``with`` body and
        released on the way out, exceptions included.  The only addition is an
        open ``trace_request`` for the live request, which is what makes a real
        run emit hop traces without any test wrapping the router.
        """

        lock = self._request_execution_lock(request_id)
        lock.acquire()
        recorder = getattr(self, "_hop_recorder", None)
        trace_context = None
        trace = None
        if recorder is not None and recorder.enabled:
            trace_context = recorder.trace_request(request_id)
            trace = trace_context.__enter__()
            active = getattr(self, "_active_hop_traces", None)
            if isinstance(active, dict):
                active[request_id] = trace
        try:
            yield
        finally:
            active = getattr(self, "_active_hop_traces", None)
            if isinstance(active, dict):
                active.pop(request_id, None)
            if trace_context is not None:
                trace_context.__exit__(None, None, None)
            lock.release()

    def _record_measured_round_trip(
        self,
        node_id: str,
        *,
        started_at: float,
        finished_at: float,
        stage_id: str,
        request_id: str | None = None,
    ) -> None:
        """File one round trip this route really performed, and emit its hop.

        ``started_at`` is read immediately before the command write and
        ``finished_at`` immediately after the correlated response arrived, so
        the difference is a measured round trip at the session boundary — not a
        pure network span, which is why it is filed under
        ``SOURCE_LIVE_SESSION_ROUND_TRIP`` rather than the socket source.

        The sample is filed under ``LOCAL_HOST_KEY`` because this process is the
        local host and the route's closed contract does not name its own stage
        node.  A caller that knows the local node id can map it with
        ``MeasuredRttTable(local_source_alias=...)`` instead of guessing.
        """

        elapsed_ms = (finished_at - started_at) * 1000.0
        if elapsed_ms < 0:
            return
        table = getattr(self, "_measured_rtt", None)
        if table is not None:
            table.record_round_trip_ms(
                LOCAL_HOST_KEY,
                node_id,
                elapsed_ms,
                measurement_source=SOURCE_LIVE_SESSION_ROUND_TRIP,
            )
        recorder = getattr(self, "_hop_recorder", None)
        if recorder is not None and recorder.enabled:
            recorder.record_hop(
                HopSample(
                    request_id=request_id or f"{stage_id}:{node_id}",
                    stage_id=stage_id,
                    node_id=node_id,
                    socket_write_at=started_at,
                    socket_read_end_at=finished_at,
                    measured_rtt_source=(
                        MEASURED_RTT_SOURCE_LIVE_SESSION_ROUND_TRIP
                    ),
                )
            )

    @property
    def execution_graph(self) -> ExecutionGraph:
        """Return the graph bound into every configured process."""
        return self._graph

    def set_stop_token_ids(self, token_ids: Sequence[int]) -> None:
        """Stop future generations before emitting a model terminator token."""

        if any(
            not isinstance(token_id, int) or isinstance(token_id, bool) or token_id < 0
            for token_id in token_ids
        ):
            raise ValueError("invalid_stop_token_ids")
        self._stop_token_ids = frozenset(token_ids)

    def set_public_projections(
        self,
        *,
        placement: Mapping[str, Any] | None,
        topology: Mapping[str, Any] | None,
        workload_comparison: Mapping[str, Any] | None = None,
    ) -> None:
        """Attach validated deployment projections to single-route status."""

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._placement_projection = (
                None if placement is None else json.loads(json.dumps(dict(placement)))
            )
            self._topology_projection = (
                None if topology is None else json.loads(json.dumps(dict(topology)))
            )
            self._workload_comparison = (
                None
                if workload_comparison is None
                else json.loads(json.dumps(dict(workload_comparison)))
            )

    def m15_plan_comparison(self) -> Mapping[str, Any] | None:
        """Return detached workload-planner intent, never route authority."""

        with self._lock:
            comparison = getattr(self, "_workload_comparison", None)
            return None if comparison is None else json.loads(json.dumps(comparison))

    def set_m17_model_operation(self, document: Mapping[str, Any] | None) -> None:
        """Attach the privacy-reduced catalog/feasibility projection."""

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._model_operation = (
                None if document is None else json.loads(json.dumps(dict(document)))
            )

    def set_deployment_qualification(
        self,
        qualification: Any,
        *,
        quantization: str,
    ) -> None:
        """Expose one qualified deployment through the same read model as a registry."""

        if qualification.deployment_id != self._graph.deployment_id:
            raise ValueError("qualification_deployment_mismatch")
        if qualification.model_id != self._graph.model_id:
            raise ValueError("qualification_model_mismatch")
        if not isinstance(quantization, str) or not quantization:
            raise ValueError("qualification_quantization_invalid")
        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._deployment_qualification = qualification
            self._deployment_quantization = quantization

    def registry_status(self) -> Mapping[str, Any]:
        """Describe a single-route server without pretending another model is selectable."""

        with self._lock:
            qualification = self._deployment_qualification
            if qualification is None:
                raise RuntimeError("deployment_qualification_unavailable")
            if self._deployment_quantization is None:
                raise RuntimeError("deployment_representation_unavailable")
            qualified = self.is_alive() and qualification.route_ready is True
            return {
                "protocol": "mycelium.live_deployment_registry.v1",
                "selected_deployment_id": self._graph.deployment_id,
                "switching_allowed": False,
                "deployments": [
                    {
                        "deployment_id": self._graph.deployment_id,
                        "model_id": self._graph.model_id,
                        "model_revision": self._graph.resolved_commit,
                        "quantization": self._deployment_quantization,
                        "topology_size": len(self._graph.stages),
                        "health": "qualified" if qualified else "unavailable",
                        "qualified_at_unix_ms": qualification.issued_at_unix_ms,
                        "qualification_id": qualification.qualification_id,
                    }
                ],
            }

    def m17_model_operation(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_model_operation", None)
            if document is None:
                return None
            if self._deployment_qualification is None:
                return json.loads(json.dumps(document))
            from mycelium_model_catalog import enrich_model_operation_lifecycle

            return enrich_model_operation_lifecycle(document, self.registry_status())

    def set_m18_replica_plan(self, document: Mapping[str, Any] | None) -> None:
        """Attach validated Planner-owned M18 replica intent."""

        from mycelium_m18_replication import validate_replica_plan

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._replica_plan = (
                None if document is None else validate_replica_plan(document)
            )

    def m18_replica_plan(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_replica_plan", None)
            return None if document is None else json.loads(json.dumps(document))

    def set_m18_replica_runtime_source(
        self, source: Callable[[], Mapping[str, Any] | None]
    ) -> None:
        if not callable(source):
            raise ValueError("m18_replica_runtime_source_invalid")
        with self._lock:
            self._m18_replica_runtime_source = source

    def m18_replica_runtime(self) -> Mapping[str, Any] | None:
        from mycelium_m18_replication import validate_replica_runtime

        source = getattr(self, "_m18_replica_runtime_source", None)
        document = None if source is None else source()
        return None if document is None else validate_replica_runtime(document)

    def set_m19_recovery_evidence(
        self,
        *,
        liveness: Mapping[str, Any] | None,
        plan: Mapping[str, Any] | None,
        runtime: Mapping[str, Any] | None,
    ) -> None:
        """Attach privacy-reduced M19 evidence; none of it grants route authority."""

        from mycelium_m19_recovery import (
            validate_liveness,
            validate_recovery_plan,
            validate_recovery_runtime,
        )

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._m19_liveness = (
                None if liveness is None else validate_liveness(liveness)
            )
            self._m19_recovery_plan = (
                None if plan is None else validate_recovery_plan(plan)
            )
            self._m19_recovery_runtime = (
                None if runtime is None else validate_recovery_runtime(runtime)
            )

    def m19_liveness(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m19_liveness", None)
            return None if document is None else json.loads(json.dumps(document))

    def m19_recovery_plan(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m19_recovery_plan", None)
            return None if document is None else json.loads(json.dumps(document))

    def m19_recovery_runtime(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m19_recovery_runtime", None)
            return None if document is None else json.loads(json.dumps(document))

    def set_m20_speculative_evidence(
        self,
        *,
        plan: Mapping[str, Any] | None,
        runtime: Mapping[str, Any] | None,
    ) -> None:
        """Attach M20 evidence without changing target-only route authority."""

        from mycelium_m20_speculation import (
            validate_speculative_plan,
            validate_speculative_runtime,
        )

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._m20_speculative_plan = (
                None if plan is None else validate_speculative_plan(plan)
            )
            self._m20_speculative_runtime = (
                None if runtime is None else validate_speculative_runtime(runtime)
            )

    def m20_speculative_plan(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m20_speculative_plan", None)
            return None if document is None else json.loads(json.dumps(document))

    def m20_speculative_runtime(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m20_speculative_runtime", None)
            return None if document is None else json.loads(json.dumps(document))

    def set_m21_heterogeneous_evidence(
        self, document: Mapping[str, Any] | None
    ) -> None:
        """Attach privacy-reduced M21 membership and physical-route evidence."""

        from mycelium_m21_heterogeneous import validate_heterogeneous_evidence

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._m21_heterogeneous = (
                None if document is None else validate_heterogeneous_evidence(document)
            )

    def m21_heterogeneous(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m21_heterogeneous", None)
            return None if document is None else json.loads(json.dumps(document))

    def set_m22_release_evidence(self, document: Mapping[str, Any] | None) -> None:
        """Attach the privacy-reduced M22 release-closure projection."""

        from mycelium_m22_release import validate_release_evidence

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._m22_release = (
                None if document is None else validate_release_evidence(document)
            )

    def m22_release(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m22_release", None)
            return None if document is None else json.loads(json.dumps(document))

    def set_m23_kv_evidence(self, document: Mapping[str, Any] | None) -> None:
        """Attach the sealed privacy-reduced M23 heterogeneous KV gate."""

        from mycelium_m23_kv import validate_m23_kv_evidence

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            self._m23_kv = (
                None if document is None else validate_m23_kv_evidence(document)
            )

    def m23_kv(self) -> Mapping[str, Any] | None:
        with self._lock:
            document = getattr(self, "_m23_kv", None)
            return None if document is None else json.loads(json.dumps(document))

    def m17_swarm_evidence(self) -> Mapping[str, Any]:
        """Capture one fresh set of independently signed node resource observations."""

        with self._lock:
            if self._closed or not self._open or not self.is_alive():
                raise RuntimeError("swarm_evidence_unavailable")
            before = len(self._signed_observations)
            self._snapshot_all()
            captured = self._signed_observations[before:]
            by_node: dict[str, dict[str, Any]] = {}
            for signed in captured:
                observation = signed.get("observation")
                if (
                    isinstance(observation, Mapping)
                    and observation.get("event") == "snapshot"
                    and isinstance(observation.get("node_id"), str)
                ):
                    by_node[str(observation["node_id"])] = signed
            if set(by_node) != set(self._sessions):
                raise RuntimeError("swarm_evidence_incomplete")
            return {
                "protocol": "mycelium.live_swarm_resource_observations.v1",
                "captured_at_unix_ms": int(time.time() * 1_000),
                "deployment_id": self._graph.deployment_id,
                "model_id": self._graph.model_id,
                "resolved_commit": self._graph.resolved_commit,
                "placement": json.loads(json.dumps(self._placement_projection)),
                "topology": json.loads(json.dumps(self._topology_projection)),
                "signed_snapshots": [by_node[node_id] for node_id in sorted(by_node)],
                "route_ready": False,
            }

    def set_m16_runtime_source(
        self,
        source: Callable[[], Mapping[str, Any] | None],
    ) -> None:
        if not callable(source):
            raise ValueError("m16_runtime_source_invalid")
        with self._lock:
            self._m16_runtime_source = source

    def m16_runtime_status(self) -> Mapping[str, Any] | None:
        # The coordinator owns its own lock. Reading the immutable source
        # reference must remain available while physical inference holds the
        # route lock, otherwise queue/admission UI would disappear precisely
        # while it is most useful.
        source = self._m16_runtime_source
        document = None if source is None else source()
        return None if document is None else json.loads(json.dumps(document))

    @property
    def startup_challenge(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Return the prompt/output pair authenticated by the controller plan."""

        return (
            tuple(self._plan["request"]["prompt_token_ids"]),
            tuple(self._plan["expected_token_ids"]),
        )

    def _node_command(self, node_id: str) -> tuple[str, ...]:
        peer = self._peers[node_id]
        if peer.process_transport == "external_iroh":
            if self._external_session_factory is None:
                raise ControllerError("external_session_provider_required")
            return ()
        if peer.process_transport == "managed_https":
            if self._session_factory is _ConcurrentNodeSession:
                raise ControllerError("managed_session_provider_required")
            return ()  # Target application owns startup; never construct remote argv.
        node_plan = self._plans_by_node[node_id]
        node_script = f"{peer.staging_root}/physical_inference_node.py"
        command = (
            *(_NODE_PROCESS_ENV_PREFIX if peer.process_transport != "local" else ()),
            node_plan["python_executable"],
            "-B",
            node_script,
            "--run-id",
            self._plan["run_id"],
            "--deployment-id",
            self._plan["deployment_id"],
            "--node-id",
            node_id,
            "--artifact-root",
            peer.staging_root,
            "--socket-root",
            node_plan["socket_root"],
            "--sidecar-binary",
            node_plan["sidecar_binary"],
            "--endpoint-secret-file",
            node_plan["endpoint_secret_file"],
            "--command-timeout",
            str(int(NODE_COMMAND_TIMEOUT_SECONDS)),
        )
        decode_mode = self._plan.get("decode_mode")
        if decode_mode is not None:
            command += ("--decode-mode", decode_mode)
        if getattr(self, "_force_relay", False):
            command += ("--sidecar-force-relay",)
        return _peer_process_argv(peer, command)

    def _external_member_key_digest(self, node_id: str) -> str:
        authority = self._seed_authority
        if authority is None:
            raise ControllerError("external_seed_authority_required")
        members = [m for m in authority.current_members() if m.get("node_id") == node_id]
        if len(members) != 1:
            raise ControllerError("external_member_identity_invalid")
        member = members[0]
        endpoint = self._endpoints[node_id]
        if (member.get("endpoint_id") != endpoint["endpoint_id"]
                or member.get("incarnation") != self._peers[node_id].boot_id
                or member.get("generation") != endpoint["membership_generation"]
                or member.get("lifecycle_state") != "RUNNING"
                or not isinstance(member.get("lease_expires_at"), (int, float))
                or not member["lease_expires_at"] > time.time()
                or not isinstance(member.get("verification_key_digest"), str)):
            raise ControllerError("external_member_identity_invalid")
        return member["verification_key_digest"]

    def _require_stage_only_positions(self, node_ids: Sequence[str]) -> None:
        for node_id in (node_ids[0], node_ids[-1]):
            if self._peers[node_id].process_transport == "external_iroh":
                raise ControllerError("external_node_stage_only")

    def _prove_external_cancellation(self, node_id: str) -> None:
        """Exercise the signed receipt before offering this route to an owner."""
        from mycelium_live.phone_stage_session import require_cancel_receipt

        command_id = self._command_id(node_id, "phone-cancel-probe")
        deadline = time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
        subject = {
            "request_id": command_id, "deployment_id": self._plan["deployment_id"],
            "deployment_epoch": self._graph.deployment_epoch,
            "qualification_digest": self._graph.manifest_digest,
            "command_id": command_id, "publisher_generation": 1,
            "absolute_deadline_ms": int(deadline * 1000), "request_attempt": 1,
            "path_id": command_id, "path_attempt": 0,
            "path_digest": self._graph.manifest_digest,
            "topology_generation": self._graph.topology_version,
            "cancellation_generation": 1,
        }
        control = {k: v for k, v in subject.items() if k != "request_id"}
        control["cancellation_generation"] = 0
        bound = self._send_before(node_id,
            command_id=self._command_id(node_id, "phone-bind-probe"),
            command="bind_request_control",
            payload={"request_id": command_id, "control": control},
            deadline_monotonic_s=deadline)
        if bound.get("ok") is not True:
            raise ControllerError("external_node_cancel_probe_bind_invalid")
        bound_observation = self._verify_observation(node_id, bound, event="request_control_bound")
        from mycelium_qualification.evidence import sha256_document
        bound_details = bound_observation.get("details")
        if (not isinstance(bound_details, Mapping)
                or bound_details.get("request_id") != command_id
                or bound_details.get("control_digest") != sha256_document(control)):
            raise ControllerError("external_node_cancel_probe_bind_invalid")
        response = self._send_before(node_id, command_id=command_id,
            command="infer_cancel_wait", payload={**subject,
                "deadline_budget_ms": mycelium_deadlines.COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS},
            deadline_monotonic_s=deadline)
        if response.get("ok") is not True:
            raise ControllerError("external_node_cancel_receipt_unsupported")
        observation = self._verify_observation(node_id, response, event="inference_cancelled")
        require_cancel_receipt(observation, subject)

    def _verify_observation(
        self,
        node_id: str,
        response: Mapping[str, Any],
        *,
        event: str,
        require_known_key: bool = True,
    ) -> dict[str, Any]:
        if self._peers[node_id].process_transport == "external_iroh":
            signed = response.get("result")
            key = signed.get("verification_key") if isinstance(signed, Mapping) else None
            if (not isinstance(key, Mapping)
                    or key.get("verification_key_digest") != self._external_member_key_digest(node_id)):
                raise ControllerError("external_member_key_mismatch")
        observation = self._controller._verified_observation(
            response,
            event=event,
            peer=self._peers[node_id],
            process_id=self._identities[node_id]["process_id"],
            run_id=self._plan["run_id"],
            deployment_id=self._plan["deployment_id"],
            endpoint_id=self._endpoints[node_id]["endpoint_id"],
            expected_verification_key=(
                self._verification_keys.get(node_id) if require_known_key else None
            ),
            signed_observation_sink=self._signed_observations,
        )
        subject = self._liveness_subjects.get(node_id)
        if subject is not None:
            self._liveness.observe_receipt(
                subject,
                observed_at_ms=int(time.monotonic() * 1_000),
                source=ObservationSource.APPLICATION_RECEIPT,
                signed=True,
            )
        return observation

    def open(self) -> RouteIdentity:
        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            if self._open:
                raise RuntimeError("route_already_open")
            try:
                self._refuse_aborted_open()
                ordered = [item["node_id"] for item in self._plan["nodes"]]
                self._require_stage_only_positions(ordered)
                if (any(p.process_transport == "external_iroh" for p in self._peers.values())
                        and self._external_session_factory is None):
                    raise ControllerError("external_session_provider_required")
                for stage in (self._graph.stages[0], self._graph.stages[-1]):
                    if any(self._peers[p.node_id].process_transport == "external_iroh"
                           for p in stage.placements):
                        raise ControllerError("external_node_stage_only")
                for node_id in sorted(self._peers):
                    external = self._peers[node_id].process_transport == "external_iroh"
                    argv = self._node_command(node_id)
                    factory = self._external_session_factory if external else self._session_factory
                    if factory is None:
                        raise ControllerError("external_session_provider_required")
                    external_arguments = ({
                        "peer": self._peers[node_id],
                        "endpoint_id": self._endpoints[node_id]["endpoint_id"],
                        "member_key_digest": self._external_member_key_digest(node_id),
                    } if external else {})
                    session = factory(
                        argv=argv,
                        node_id=node_id,
                        run_id=self._plan["run_id"],
                        deployment_id=self._plan["deployment_id"],
                        timeout_seconds=NODE_SESSION_TIMEOUT_SECONDS,
                        **external_arguments,
                    )
                    self._sessions[node_id] = session
                    self._refuse_aborted_open()
                    hello = session.send(
                        command_id=self._command_id(node_id, "hello"),
                        command="hello",
                        payload={},
                    )
                    self._identities[node_id] = self._controller._hello_identity(
                        hello,
                        peer=self._peers[node_id],
                        run_id=self._plan["run_id"],
                        deployment_id=self._plan["deployment_id"],
                    )
                    subject = LivenessSubject(
                        subject_id=node_id,
                        kind=SubjectKind.PEER,
                        membership_generation=int(
                            self._endpoints[node_id]["membership_generation"]
                        ),
                    )
                    self._liveness_subjects[node_id] = subject
                    self._liveness.register_subject(
                        subject,
                        observed_at_ms=int(time.monotonic() * 1_000),
                    )

                for node_id in sorted(self._sessions):
                    self._refuse_aborted_open()
                    response = self._sessions[node_id].send(
                        command_id=self._command_id(node_id, "configure"),
                        command="configure",
                        payload={**self._plans_by_node[node_id]["configure"], **({
                            "request_control_graph": {
                                "deployment_id": self._graph.deployment_id,
                                "deployment_epoch": self._graph.deployment_epoch,
                                "topology_generation": self._graph.topology_version,
                            }
                        } if self._peers[node_id].process_transport == "external_iroh" else {})},
                    )
                    observation = self._verify_observation(
                        node_id,
                        response,
                        event="configured",
                        require_known_key=False,
                    )
                    endpoint_address = observation["details"].get("endpoint_addr")
                    if (
                        not isinstance(endpoint_address, Mapping)
                        or endpoint_address.get("id")
                        != self._endpoints[node_id]["endpoint_id"]
                    ):
                        raise RuntimeError("node_endpoint_address_invalid")
                    self._endpoint_addresses[node_id] = dict(endpoint_address)
                    self._verification_keys[node_id] = dict(
                        response["result"]["verification_key"]
                    )

                # The run-plan order is the selected physical cycle. Sorting node
                # ids here silently replaced an evidence-driven M14 route with a
                # canonical route whenever the optimizer chose a different cycle.
                ordered = [item["node_id"] for item in self._plan["nodes"]]
                if len(ordered) != len(set(ordered)) or set(ordered) != set(
                    self._sessions
                ):
                    raise RuntimeError("route_plan_node_order_invalid")
                successors = {
                    node_id: ordered[(index + 1) % len(ordered)]
                    for index, node_id in enumerate(ordered)
                }
                for node_id in ordered:
                    self._refuse_aborted_open()
                    remote_node_id = successors[node_id]
                    additional_node_ids = [
                        candidate
                        for candidate in ordered
                        if candidate not in {node_id, remote_node_id}
                    ]

                    def peer_document(candidate: str) -> dict[str, Any]:
                        return {
                            "node_id": candidate,
                            "endpoint_id": self._endpoints[candidate]["endpoint_id"],
                            "endpoint_addr": self._endpoint_addresses[candidate],
                            "generation": self._endpoints[candidate][
                                "membership_generation"
                            ],
                        }

                    response = self._sessions[node_id].send(
                        command_id=self._command_id(node_id, "start"),
                        command="start",
                        payload={
                            "peer": peer_document(remote_node_id),
                            "peers": [
                                peer_document(candidate)
                                for candidate in additional_node_ids
                            ],
                            "local_generation": self._endpoints[node_id][
                                "membership_generation"
                            ],
                        },
                    )
                    self._verify_observation(node_id, response, event="started")
                for node_id in ordered:
                    if self._peers[node_id].process_transport == "external_iroh":
                        self._prove_external_cancellation(node_id)
                self._refuse_aborted_open()
                self._open = True
                _register_open_route(self)
                self._liveness_monitor_stop.clear()
                self._liveness_monitor_thread = threading.Thread(
                    target=self._active_liveness_monitor_loop,
                    name="mycelium-active-liveness-monitor",
                    daemon=True,
                )
                self._liveness_monitor_thread.start()
            except BaseException as exc:
                self._fatal = getattr(exc, "code", type(exc).__name__)
                self._record_incident(
                    state="route_failed_closed",
                    reason=str(self._fatal),
                    request_id=None,
                )
                self.close()
                raise

            graph = self._graph
            return RouteIdentity(
                deployment_id=graph.deployment_id,
                model_id=graph.model_id,
                resolved_commit=graph.resolved_commit,
                endpoint_ids=tuple(
                    self._endpoints[node_id]["endpoint_id"]
                    for node_id in sorted(self._endpoints)
                ),
            )

    @staticmethod
    def _output_tokens(observation: Mapping[str, Any]) -> tuple[int, ...]:
        output = observation.get("details", {}).get("output")
        if not isinstance(output, Mapping) or not isinstance(
            output.get("token_ids"), list
        ):
            raise RuntimeError("node_inference_output_invalid")
        tokens = tuple(output["token_ids"])
        if not all(
            isinstance(token, int) and not isinstance(token, bool) and token >= 0
            for token in tokens
        ):
            raise RuntimeError("node_inference_output_invalid")
        return tokens

    def _snapshot_all(self) -> None:
        self._snapshot_nodes(frozenset(self._sessions))

    def _snapshot_nodes(
        self,
        node_ids: frozenset[str],
        *,
        deadline_monotonic_s: float | None = None,
    ) -> None:
        snapshots: dict[str, dict[str, Any]] = {}
        if not node_ids or not node_ids <= set(self._sessions):
            raise ValueError("snapshot_nodes_invalid")
        for node_id in sorted(node_ids):
            if deadline_monotonic_s is None:
                response = self._sessions[node_id].send(
                    command_id=self._command_id(node_id, "snapshot"),
                    command="snapshot",
                    payload={},
                )
            else:
                response = self._send_before(
                    node_id,
                    command_id=self._command_id(node_id, "snapshot"),
                    command="snapshot",
                    payload={},
                    deadline_monotonic_s=deadline_monotonic_s,
                )
            observation = self._verify_observation(node_id, response, event="snapshot")
            snapshots[node_id] = observation
            self._ingest_transport_scoped_events(node_id, observation)
        with self._lock:
            self._last_snapshots.update(snapshots)

    def _monitor_active_liveness_once(self) -> None:
        """Observe active data paths and interrupt only their failed tracks."""

        observed_at_s = time.monotonic()
        with self._lock:
            if not self._open or self._closed:
                return
            active_node_ids = frozenset(
                node_id
                for active in self._active_route_requests.values()
                if (
                    not active.get("terminal")
                    and not active.get("cancellation_requested")
                )
                for node_id in active.get("participating_node_ids", ())
            )
        if not active_node_ids:
            return

        def health_active_node(node_id: str) -> bool:
            try:
                response = self._send_before(
                    node_id,
                    command_id=self._command_id(node_id, "health"),
                    command="health",
                    payload={},
                    deadline_monotonic_s=observed_at_s + 0.5,
                )
                observation = self._verify_observation(
                    node_id,
                    response,
                    event="health",
                )
                with self._lock:
                    self._last_health[node_id] = observation
            except BaseException:
                return False
            return True

        with ThreadPoolExecutor(
            max_workers=len(active_node_ids),
            thread_name_prefix="a4-liveness-snapshot",
        ) as executor:
            health_results = tuple(
                executor.map(health_active_node, sorted(active_node_ids))
            )
        if not any(health_results):
            self._handle_lost_peer_processes()
            return
        if not all(health_results):
            # A participant that stopped answering health may be an exited
            # process (a lost pipeline stage while its peers still answer).
            # Settle it within this tick, not at the next status read: A7
            # hands an exited non-entry participant to its request-scoped
            # successor; otherwise the request is interrupted inside the
            # cancellation and cleanup budget instead of blocking on the
            # entry's in-flight command until its node command timeout.
            self._handle_lost_peer_processes()
        with self._lock:
            failed_nodes: dict[str, str] = {}
            for node_id in active_node_ids:
                details = self._last_health.get(node_id, {}).get("details")
                sidecar = (
                    details.get("sidecar_process")
                    if isinstance(details, Mapping)
                    else None
                )
                if (
                    isinstance(sidecar, Mapping)
                    and sidecar.get("started") is True
                    and sidecar.get("alive") is False
                ):
                    failed_nodes[node_id] = "sidecar_process_exited"
                    continue
                fatal = (
                    details.get("transport_fatal_error")
                    if isinstance(details, Mapping)
                    else None
                )
                if isinstance(fatal, Mapping):
                    code = fatal.get("code")
                    failed_nodes[node_id] = (
                        code if isinstance(code, str) and code else "transport_failed"
                    )
            affected_by_node = {
                node_id: tuple(
                    sorted(
                        request_id
                        for request_id, active in self._active_route_requests.items()
                        if not active.get("terminal")
                        and not active.get("cancellation_requested")
                        and node_id in active.get("participating_node_ids", ())
                    )
                )
                for node_id in failed_nodes
            }
        for node_id, reason in sorted(failed_nodes.items()):
            affected_tracks = affected_by_node[node_id]
            if not affected_tracks:
                continue
            subject = self._liveness_subjects.get(node_id)
            if subject is not None:
                failure_started_at_ms = int(observed_at_s * 1_000)
                subject_snapshot = self._liveness.subject_snapshot(subject)
                observed_at_ms = max(
                    int(time.monotonic() * 1_000),
                    (
                        subject_snapshot.last_observed_ms + 1
                        if subject_snapshot is not None
                        else failure_started_at_ms
                    ),
                )
                try:
                    self._liveness.record_active_failure(
                        subject,
                        failure_started_at_ms=failure_started_at_ms,
                        observed_at_ms=observed_at_ms,
                        scope="request",
                        affected_track_ids=affected_tracks,
                        verified=True,
                    )
                except BaseException:
                    # Evidence projection must never suppress bounded safety work.
                    pass
            interruption_deadline = observed_at_s + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
            for request_id in affected_tracks:
                try:
                    self._record_scoped_runtime_incident(
                        request_id=request_id,
                        subject_id=node_id,
                        scope="request",
                        reason=reason,
                    )
                except BaseException:
                    # Preserve cancellation even if incident validation rejects.
                    pass
                try:
                    self.cancel_request(
                        request_id,
                        deadline_monotonic_s=interruption_deadline,
                    )
                except BaseException:
                    # Inference owner still performs the same deadline-bound
                    # cleanup path. Keep this monitor available for other peers.
                    pass

    def _active_liveness_monitor_loop(self) -> None:
        while not self._liveness_monitor_stop.wait(0.05):
            try:
                self._monitor_active_liveness_once()
                self._monitor_idle_keepalives_once()
            except BaseException:
                if self._liveness_monitor_stop.is_set():
                    return
                self._handle_lost_peer_processes()

    def _monotonic_ms(self) -> int:
        """Injectable monotonic clock (milliseconds) for liveness observation."""

        return int(time.monotonic() * 1_000)

    def _monitor_idle_keepalives_once(self) -> None:
        """Probe each peer whose keepalive is due; a silent peer accrues misses.

        The detector's traffic-aware design expects an explicit idle probe:
        a verified response is a fresh application receipt (it extends the
        keepalive due horizon), while a probe that times out or fails is one
        recorded keepalive miss.  Suspect after the first miss, quarantine
        after QUARANTINE_MISSES with QUARANTINE_STALE_MS elapsed.  This runs
        even when no requests are active, which is what makes an idle-stalled
        participating peer observable at all.

        A peer participating in a non-terminal request is not idle.  Its
        active-failure monitor, command deadline, or cancellation-cleanup
        owner is authoritative until that request reaches terminal state.
        Sending an idle probe through the same bounded control lane during
        that interval can time out behind legitimate work and must not be
        misclassified as an idle-loss receipt miss.
        """

        observed_at_ms = self._monotonic_ms()
        with self._lock:
            if not self._open or self._closed:
                return
            subjects = tuple(sorted(self._liveness_subjects.items()))
            busy_node_ids = frozenset(
                node_id
                for active in self._active_route_requests.values()
                if not active.get("terminal")
                for node_id in active.get("participating_node_ids", ())
            )
        for node_id, subject in subjects:
            if node_id in busy_node_ids:
                continue
            decision = self._liveness.keepalive_due(
                subject,
                observed_at_ms=observed_at_ms,
            )
            if not decision.accepted or not decision.due:
                continue
            if self._idle_probe_peer(node_id):
                continue
            self._liveness.record_keepalive_miss(
                subject,
                observed_at_ms=self._monotonic_ms(),
            )

    def _idle_probe_peer(self, node_id: str) -> bool:
        """Send one bounded health probe; True when the peer answered fresh.

        A verified response refreshes the subject via the ordinary receipt
        path in _verify_observation.  Any failure (deadline, rejection, lost
        process) counts as a silent peer for this keepalive window.
        """

        try:
            # This probe is the route's continuous RTT sampler: it runs on every
            # keepalive window for every peer whose keepalive is due, so idle
            # links are re-measured without waiting for user traffic.  Before
            # this, the duration was discarded and no RTT sample was ever filed.
            probe_started_at = time.monotonic()
            # A peer reached through a polled mailbox (managed control) needs a claim hop, the execution and a
            # result hop: 0.5 s is its whole wire life and expired on every relayed probe (live demo
            # 2026-10-20/21), which kept a quarantined peer quarantined.  The session declares its floor; active
            # request liveness keeps its own bounds.
            probe_deadline = max(
                _IDLE_KEEPALIVE_PROBE_DEADLINE_SECONDS,
                float(getattr(self._sessions.get(node_id), "idle_probe_floor_seconds", 0.0) or 0.0),
            )
            response = self._send_before(
                node_id,
                command_id=self._command_id(node_id, "keepalive"),
                command="health",
                payload={},
                deadline_monotonic_s=time.monotonic() + probe_deadline,
            )
            self._record_measured_round_trip(
                node_id,
                started_at=probe_started_at,
                finished_at=time.monotonic(),
                stage_id="keepalive",
            )
            observation = self._verify_observation(
                node_id,
                response,
                event="health",
            )
            with self._lock:
                self._last_health[node_id] = observation
            return True
        except BaseException:
            return False

    def _peer_subject_is_quarantined(self, node_id: str) -> bool:
        subjects = getattr(self, "_liveness_subjects", None)
        subject = subjects.get(node_id) if isinstance(subjects, dict) else None
        if subject is None:
            return False
        snapshot = self._liveness.subject_snapshot(subject)
        return snapshot is not None and snapshot.state is LivenessState.QUARANTINED

    def _reject_if_affected_peer_quarantined(
        self,
        participating_node_ids: frozenset[str],
    ) -> None:
        """Fail closed: admission touching a quarantined peer is refused.

        The detector's quarantine incident carries the
        ``remove_from_affected_admission`` action; this is its enforcement
        point for new requests whose participating nodes include the
        quarantined peer.
        """

        quarantined = tuple(
            sorted(
                node_id
                for node_id in participating_node_ids
                if self._peer_subject_is_quarantined(node_id)
            )
        )
        if quarantined:
            raise AffectedPeerQuarantined("affected_peer_quarantined")

    def _ingest_transport_scoped_events(
        self,
        node_id: str,
        observation: Mapping[str, Any],
    ) -> None:
        details = observation.get("details")
        transport = details.get("transport") if isinstance(details, Mapping) else None
        events = (
            transport.get("scoped_events") if isinstance(transport, Mapping) else None
        )
        if not isinstance(events, list):
            return
        last_sequence = self._last_transport_event_sequence.get(node_id, 0)
        for event in events:
            if not isinstance(event, Mapping):
                continue
            sequence = event.get("sequence")
            request_id = event.get("request_id")
            path_id = event.get("path_id")
            path_attempt = event.get("path_attempt")
            peer_node_id = event.get("peer_node_id")
            peer_generation = event.get("peer_generation")
            event_kind = event.get("event")
            if (
                type(sequence) is not int
                or sequence <= last_sequence
                or not isinstance(request_id, str)
                or not request_id
                or not isinstance(path_id, str)
                or not path_id
                or type(path_attempt) is not int
                or path_attempt < 0
                or not isinstance(peer_node_id, str)
                or not peer_node_id
                or type(peer_generation) is not int
                or peer_generation < 1
                or event_kind not in {"receipt", "failure"}
            ):
                continue
            edge_id = f"{node_id}->{peer_node_id}"
            subject = self._liveness_edge_subjects.get(edge_id)
            if subject is None or subject.membership_generation != peer_generation:
                observed_at_ms = int(time.monotonic() * 1_000)
                subject = LivenessSubject(
                    subject_id=edge_id,
                    kind=SubjectKind.EDGE,
                    membership_generation=peer_generation,
                )
                registered = self._liveness.register_subject(
                    subject,
                    observed_at_ms=max(0, observed_at_ms - 1),
                )
                if not registered.accepted:
                    continue
                self._liveness_edge_subjects[edge_id] = subject
            observed_at_ms = int(time.monotonic() * 1_000)
            if event_kind == "receipt":
                self._liveness.observe_receipt(
                    subject,
                    observed_at_ms=observed_at_ms,
                    source=ObservationSource.APPLICATION_RECEIPT,
                    signed=True,
                )
            else:
                self._liveness.record_active_failure(
                    subject,
                    failure_started_at_ms=observed_at_ms,
                    observed_at_ms=observed_at_ms,
                    scope="edge",
                    affected_track_ids=(request_id,),
                    verified=True,
                )
                with self._lock:
                    active = self._active_route_requests.get(request_id)
                    exact_active_identity = bool(
                        active is not None
                        and active.get("path_id") == path_id
                        and active.get("path_attempt") == path_attempt
                    )
                if exact_active_identity:
                    self._record_scoped_runtime_incident(
                        request_id=request_id,
                        subject_id=edge_id,
                        scope="edge",
                        reason=(
                            event.get("code")
                            if isinstance(event.get("code"), str) and event.get("code")
                            else "transport_failure"
                        ),
                    )
            last_sequence = sequence
        self._last_transport_event_sequence[node_id] = last_sequence

    def _send_before(
        self,
        node_id: str,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
        deadline_monotonic_s: float,
    ) -> dict[str, Any]:
        sender = getattr(self._sessions[node_id], "send_before", None)
        if callable(sender):
            return sender(
                command_id=command_id,
                command=command,
                payload=payload,
                deadline_monotonic_s=deadline_monotonic_s,
            )
        if time.monotonic() >= deadline_monotonic_s:
            raise TimeoutError("command_deadline_exceeded")
        return self._sessions[node_id].send(
            command_id=command_id,
            command=command,
            payload=payload,
        )

    def _snapshot_nodes_before(
        self,
        node_ids: frozenset[str],
        *,
        deadline_monotonic_s: float,
        cleanup_subject: Mapping[str, Any] | None = None,
        receipt_only: bool = False,
    ) -> None:
        def snapshot_node(node_id: str) -> tuple[str, dict[str, Any]]:
            # Data-plane failure does not prove resource cleanup. The control
            # channel may still answer; require a fresh verified observation.
            try:
                response = self._send_before(
                    node_id,
                    command_id=self._command_id(node_id, "snapshot"),
                    command="snapshot",
                    payload=(
                        {}
                        if cleanup_subject is None
                        else {
                            "cleanup_subject": dict(cleanup_subject),
                            "receipt_only": receipt_only,
                        }
                    ),
                    deadline_monotonic_s=deadline_monotonic_s,
                )
            except BaseException as error:
                code = str(getattr(error, "code", str(error) or type(error).__name__))
                raise RuntimeError(f"{code}@{node_id}") from error
            observation = self._verify_observation(
                node_id,
                response,
                event="snapshot",
            )
            self._ingest_transport_scoped_events(node_id, observation)
            return node_id, observation

        # Every proof command shares the owner's original absolute deadline.
        # Parallel fanout prevents one slower peer from consuming another peer's
        # proof budget while retaining one independently verified receipt per node.
        errors: list[BaseException] = []
        with ThreadPoolExecutor(
            max_workers=len(node_ids),
            thread_name_prefix="a4-cleanup-snapshot",
        ) as executor:
            futures = {
                executor.submit(snapshot_node, node_id): node_id
                for node_id in sorted(node_ids)
            }
            for future in as_completed(futures):
                try:
                    node_id, observation = future.result()
                except BaseException as error:
                    errors.append(error)
                    continue
                # Commit each independently verified receipt immediately.
                # One delayed sibling must not erase authority that already
                # returned before the shared absolute deadline.  An explicit
                # fallback snapshot can race the inline ``infer_cancel_wait``
                # receipt for the same node.  Once either observer has proved
                # complete cleanup, a stale incomplete response is not allowed
                # to move that request-scoped proof backwards.
                with self._lock:
                    # Receipt-only observations intentionally omit general
                    # runtime/transport capability fields.  Keep them solely
                    # in the request-scoped proof ledger so a fast committed-
                    # receipt response cannot erase the last full live-status
                    # projection for this node.
                    if not receipt_only:
                        self._last_snapshots[node_id] = observation
                    if cleanup_subject is not None:
                        request_id = cleanup_subject.get("request_id")
                        if isinstance(request_id, str) and request_id:
                            scoped_by_request = getattr(
                                self, "_cleanup_snapshots_by_request", None
                            )
                            if scoped_by_request is None:
                                scoped_by_request = {}
                                self._cleanup_snapshots_by_request = scoped_by_request
                            request_snapshots = scoped_by_request.setdefault(
                                request_id, {}
                            )
                            existing = request_snapshots.get(node_id)

                            def cleanup_complete(
                                candidate: object,
                            ) -> bool:
                                details = (
                                    candidate.get("details")
                                    if isinstance(candidate, Mapping)
                                    else None
                                )
                                receipt = (
                                    details.get("request_cleanup")
                                    if isinstance(details, Mapping)
                                    else None
                                )
                                return (
                                    isinstance(receipt, Mapping)
                                    and receipt.get("complete") is True
                                )

                            if not cleanup_complete(existing) or cleanup_complete(
                                observation
                            ):
                                request_snapshots[node_id] = observation
        if errors:
            raise errors[0]

    def _cleanup_blockers(
        self,
        participating_node_ids: frozenset[str],
        cleanup_subject: Mapping[str, Any],
    ) -> str:
        """Return one bounded, privacy-reduced cleanup diagnostic."""

        request_id = cleanup_subject.get("request_id")
        with self._lock:
            scoped = dict(
                getattr(self, "_cleanup_snapshots_by_request", {}).get(request_id, {})
                if isinstance(request_id, str)
                else {}
            )
            active = (
                self._active_route_requests.get(request_id)
                if isinstance(request_id, str)
                else None
            )
            fanout_error = (
                active.get("cancellation_fanout_error")
                if isinstance(active, Mapping)
                else None
            )
        blockers: list[str] = []
        if isinstance(fanout_error, str) and fanout_error:
            blockers.append(f"fanout={fanout_error[:128]}")
        for node_id in sorted(participating_node_ids):
            observation = scoped.get(node_id)
            details = (
                observation.get("details") if isinstance(observation, Mapping) else None
            )
            receipt = (
                details.get("request_cleanup") if isinstance(details, Mapping) else None
            )
            if not isinstance(receipt, Mapping):
                blockers.append(f"{node_id}=receipt_missing")
                continue
            reasons = [
                key
                for key in (
                    "runtime_clean",
                    "transport_clean",
                    "cancellation_worker_complete",
                    "complete",
                )
                if receipt.get(key) is not True
            ]
            reasons.extend(
                f"identity.{field}"
                for field, expected in cleanup_subject.items()
                if receipt.get(field) != expected
            )
            transport_state = receipt.get("transport_state")
            if isinstance(transport_state, Mapping):
                reasons.extend(
                    f"transport.{key}={value}"
                    for key, value in sorted(transport_state.items())
                    if key != "cancellation_observed" and value not in (0, False)
                )
            worker_error = receipt.get("cancellation_worker_error")
            if isinstance(worker_error, str) and worker_error:
                reasons.append(f"worker_error={worker_error[:128]}")
            blockers.append(f"{node_id}=" + (",".join(reasons) if reasons else "clean"))
        return ";".join(blockers)[:512]

    def _cancellation_cleanup_complete(
        self,
        participating_node_ids: frozenset[str] | None = None,
        *,
        cleanup_subject: Mapping[str, Any] | None = None,
    ) -> bool:
        required = (
            frozenset(self._sessions)
            if participating_node_ids is None
            else participating_node_ids
        )
        with self._lock:
            if cleanup_subject is None:
                snapshots = dict(self._last_snapshots)
            else:
                request_id = cleanup_subject.get("request_id")
                route_receipt = (
                    getattr(self, "_request_cleanup_receipts", {}).get(request_id)
                    if isinstance(request_id, str)
                    else None
                )
                if isinstance(route_receipt, Mapping):
                    receipt_payload = {
                        key: value
                        for key, value in route_receipt.items()
                        if key != "receipt_digest"
                    }
                    expected_digest = (
                        "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                receipt_payload,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                    )
                    receipt_nodes = route_receipt.get("node_ids")
                    if (
                        all(
                            route_receipt.get(field) == expected
                            for field, expected in cleanup_subject.items()
                        )
                        and isinstance(receipt_nodes, list)
                        and all(isinstance(node_id, str) for node_id in receipt_nodes)
                        and required <= frozenset(receipt_nodes)
                        and route_receipt.get("cleanup_owner_id")
                        == (
                            "physical-live-route:"
                            f"{cleanup_subject.get('deployment_id')}"
                        )
                        and route_receipt.get("receipt_digest") == expected_digest
                    ):
                        # This aggregate exists only after exact, signed,
                        # request-scoped receipts were verified for every
                        # participating node.  It remains the route's cleanup
                        # authority until scoped release.  Concurrent terminal
                        # and cancellation waiters must not forget that proof
                        # merely because _store_request_cleanup_receipt retired
                        # the heavier per-node snapshots; re-querying the node
                        # after that point can race its bounded receipt cache
                        # and incorrectly project clean resources as
                        # complete=False after the generation proof was already
                        # sealed here.
                        return True
                scoped_by_request = getattr(self, "_cleanup_snapshots_by_request", {})
                snapshots = dict(
                    scoped_by_request.get(request_id, {})
                    if isinstance(request_id, str)
                    else {}
                )
        if not required or not required <= set(snapshots):
            return False
        for node_id in required:
            observation = snapshots[node_id]
            details = observation.get("details")
            if not isinstance(details, Mapping):
                return False
            # Fatal transport health is not an exact cleanup receipt and
            # cannot waive runtime/transport state or subject checks below.
            if cleanup_subject is None:
                runtime = details.get("runtime")
                if (
                    not isinstance(runtime, Mapping)
                    or runtime.get("active_state_count") != 0
                    or details.get("transport_pending_delivery_count") != 0
                    or details.get("transport_cancellation_cleanup_complete")
                    is not True
                ):
                    return False
            else:
                receipt = details.get("request_cleanup")
                if (
                    not isinstance(receipt, Mapping)
                    or any(
                        receipt.get(field) != expected
                        for field, expected in cleanup_subject.items()
                    )
                    or receipt.get("complete") is not True
                ):
                    return False
        return True

    def _wait_for_cancellation_cleanup(
        self,
        participating_node_ids: frozenset[str],
        *,
        deadline_monotonic_s: float,
        cleanup_subject: Mapping[str, Any],
        receipt_only: bool = False,
    ) -> None:
        request_id = cleanup_subject.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise RuntimeError("route_cleanup_subject_invalid")

        def cleanup_complete() -> bool:
            complete = self._cancellation_cleanup_complete(
                participating_node_ids,
                cleanup_subject=cleanup_subject,
            )
            if complete:
                # Receipt publication is part of the cancellation lifecycle,
                # not a later best-effort epilogue in the inference worker.
                # This makes a late exact proof capable of reviving the
                # gateway terminal even if the original worker has already
                # unwound through its fail-closed path.
                self._seal_and_publish_request_cleanup_receipt(
                    request_id=request_id,
                    cleanup_subject=cleanup_subject,
                    participating_node_ids=participating_node_ids,
                )
            return complete

        with self._lock:
            active = self._active_route_requests.get(request_id)
            fanout_complete = (
                None if active is None else active.get("cancellation_fanout_complete")
            )
            cancellation_requested = bool(
                active is not None and active.get("cancellation_requested") is True
            )
            cancellation_started_at = (
                None if active is None else active.get("cancellation_started_at")
            )
        inline_receipt_grace_deadline = time.monotonic()
        if isinstance(cancellation_started_at, (int, float)) and not isinstance(
            cancellation_started_at,
            bool,
        ):
            inline_receipt_grace_deadline = min(
                deadline_monotonic_s,
                float(cancellation_started_at) + _CLEANUP_INLINE_RECEIPT_GRACE_SECONDS,
            )
        if cancellation_requested:
            if not isinstance(fanout_complete, threading.Event):
                raise RuntimeError("route_cancellation_fanout_untracked")
            if cleanup_complete():
                return
            # ``infer_cancel_wait`` gets one bounded grace interval to return
            # the lifecycle receipt without a duplicate observer.  Do not,
            # however, wait for the global fanout barrier after that interval:
            # one delayed leaf otherwise monopolizes 1.5 seconds of the fixed
            # two-second owner deadline and leaves its exact fallback snapshot
            # only one fragile 0.5-second attempt.  After the node's own
            # receipt wait has elapsed, bounded request-scoped snapshots may
            # race the delayed response; monotonic receipt storage makes that
            # race safe and preserves the original deadline.
        while True:
            if cleanup_complete():
                return
            if (
                cancellation_requested
                and not fanout_complete.is_set()
                and time.monotonic() < inline_receipt_grace_deadline
            ):
                remaining = deadline_monotonic_s - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "route_cancellation_cleanup_timeout|"
                        f"{self._cleanup_blockers(participating_node_ids, cleanup_subject)}"
                    )
                fanout_complete.wait(
                    timeout=min(
                        _CLEANUP_RECEIPT_POLL_SECONDS,
                        remaining,
                        inline_receipt_grace_deadline - time.monotonic(),
                    )
                )
                continue
            missing_node_ids = frozenset(
                node_id
                for node_id in participating_node_ids
                if not self._cancellation_cleanup_complete(
                    frozenset({node_id}),
                    cleanup_subject=cleanup_subject,
                )
            )
            if not missing_node_ids:
                if cleanup_complete():
                    return
                continue
            # A participant whose process has exited can never answer a cleanup
            # snapshot, and asking it fails at once (``node_process_closed``).
            # That failure must not end the wait while a live participant is
            # still converging (a survivor busy on its stage answers the
            # cancellation after the inline-receipt grace): the router's
            # dead-peer proof needs every survivor's exact receipt, so ending
            # here strands the request in ``request_cleanup_unproven`` even
            # though the survivor proves clean moments later.  Keep proving the
            # live participants inside the same owner deadline; once only
            # exited participants lack a receipt, report exactly that.  An
            # exited peer's receipt is never waived here: the route still
            # raises, and only the router's identity-bound dead-peer proof can
            # seal its cleanup.
            exited_node_ids = self._exited_participant_ids(missing_node_ids)
            live_missing_node_ids = missing_node_ids - exited_node_ids
            if not live_missing_node_ids:
                raise RuntimeError(
                    "route_cleanup_participant_exited|"
                    f"{self._cleanup_blockers(participating_node_ids, cleanup_subject)}"
                )
            try:
                attempt_deadline = min(
                    deadline_monotonic_s,
                    time.monotonic() + _CLEANUP_SNAPSHOT_SLICE_SECONDS,
                )
                self._snapshot_nodes_before(
                    live_missing_node_ids,
                    deadline_monotonic_s=attempt_deadline,
                    cleanup_subject=cleanup_subject,
                    receipt_only=receipt_only,
                )
            except BaseException as error:
                # The fanout and explicit proof round share the same owner
                # deadline. An inline signed receipt can commit while one
                # redundant snapshot response is timing out; re-read the
                # request-scoped proof set before classifying cleanup as
                # unproven.
                if cleanup_complete():
                    return
                # One delayed observer must not monopolize the owner's entire
                # cleanup deadline.  Retry any attempt that expired against the
                # deadline it was handed (local slice guard or response
                # channel), in bounded rounds, while owner time remains, as long
                # as no proof has been committed; malformed, unsigned,
                # mismatched, or explicitly incomplete proof remains
                # fail-closed below.  `command_deadline_exceeded@<node>` is the
                # local slice guard, not the owner deadline: measured on the
                # two-Mac route it discarded 0.987 s of live owner window in
                # which the peer's receipt was already provable (round 52 §3).
                # Late command IDs stay retired by PhysicalNodeProcess, so a
                # retry cannot be confused with the prior observation.
                if (
                    any(
                        code in str(error)
                        for code in _CLEANUP_RETRYABLE_ATTEMPT_CODES
                    )
                    and time.monotonic() < deadline_monotonic_s
                ):
                    # Bounded step-back: an attempt that fails without
                    # consuming its slice must not spin the owner's worker.
                    time.sleep(
                        min(
                            _CLEANUP_RECEIPT_POLL_SECONDS,
                            max(0.0, deadline_monotonic_s - time.monotonic()),
                        )
                    )
                    continue
                # A participant that exited during this attempt answered with
                # its closed control channel, not with a cleanup verdict; the
                # next round proves the remaining live participants (above).
                if (
                    self._exited_participant_ids(live_missing_node_ids)
                    and time.monotonic() < deadline_monotonic_s
                ):
                    continue
                raise RuntimeError(
                    f"{str(error)[:128]}|"
                    f"{self._cleanup_blockers(participating_node_ids, cleanup_subject)}"
                ) from error
            if cleanup_complete():
                return
            if time.monotonic() >= deadline_monotonic_s:
                raise RuntimeError(
                    "route_cancellation_cleanup_timeout|"
                    f"{self._cleanup_blockers(participating_node_ids, cleanup_subject)}"
                )
            # Cleanup receipts are request-specific. A bounded cadence leaves
            # the node control lane available for the other concurrently
            # cancelling owners instead of queuing hundreds of already-stale
            # proof commands behind the same two-second deadline.
            remaining = deadline_monotonic_s - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "route_cancellation_cleanup_timeout|"
                    f"{self._cleanup_blockers(participating_node_ids, cleanup_subject)}"
                )
            time.sleep(min(_CLEANUP_RECEIPT_POLL_SECONDS, remaining))

    def _store_request_cleanup_receipt(
        self,
        *,
        request_id: str,
        cleanup_subject: Mapping[str, Any],
        participating_node_ids: frozenset[str],
        deployment_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            receipts = getattr(self, "_request_cleanup_receipts", None)
            if receipts is None:
                receipts = {}
                self._request_cleanup_receipts = receipts
            existing = receipts.get(request_id)
            if existing is not None:
                if any(
                    existing.get(field) != value
                    for field, value in cleanup_subject.items()
                ):
                    raise RuntimeError("route_cleanup_receipt_identity_mismatch")
                return dict(existing)
            receipt = {
                **cleanup_subject,
                "cleanup_owner_id": f"physical-live-route:{deployment_id}",
                "node_ids": sorted(participating_node_ids),
                "completed_at_monotonic_ms": int(time.monotonic() * 1_000),
            }
            receipt["receipt_digest"] = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        receipt,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
            self._request_cleanup_receipts[request_id] = receipt
            getattr(self, "_cleanup_snapshots_by_request", {}).pop(request_id, None)
            return dict(receipt)

    def _seal_and_publish_request_cleanup_receipt(
        self,
        *,
        request_id: str,
        cleanup_subject: Mapping[str, Any],
        participating_node_ids: frozenset[str],
    ) -> dict[str, Any] | None:
        """Seal and publish one aggregate receipt exactly once.

        Per-node signed receipts are committed by either ``infer_cancel_wait``
        or the bounded snapshot fallback.  Publishing their aggregate in this
        proof-owning lifecycle closes the ordering gap where physical cleanup
        reached zero, the inference worker returned, and the gateway retained
        reservations forever because no later component owned publication.
        """

        if not self._cancellation_cleanup_complete(
            participating_node_ids,
            cleanup_subject=cleanup_subject,
        ):
            return None
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None:
                return None
            publisher = active.get("publish_cleanup_receipt")
            if not callable(publisher):
                return None
            publication_event = active.get("cleanup_receipt_publication_event")
            if not isinstance(publication_event, threading.Event):
                publication_event = threading.Event()
                active["cleanup_receipt_publication_event"] = publication_event
            already_published = active.get("cleanup_receipt_published") is True
            publication_started = (
                active.get("cleanup_receipt_publication_started") is True
            )
            deployment_id = active.get("deployment_id")
            if not isinstance(deployment_id, str) or not deployment_id:
                raise RuntimeError("route_cleanup_deployment_identity_missing")
            receipt = self._store_request_cleanup_receipt(
                request_id=request_id,
                cleanup_subject=cleanup_subject,
                participating_node_ids=participating_node_ids,
                deployment_id=deployment_id,
            )
            if already_published:
                return receipt
            if publication_started:
                publish_here = False
            else:
                active["cleanup_receipt_publication_started"] = True
                publish_here = True
        if not publish_here:
            publication_event.wait()
            with self._lock:
                active = self._active_route_requests.get(request_id)
                if active is None or active.get("cleanup_receipt_published") is True:
                    return receipt
            raise RuntimeError("route_cleanup_receipt_publication_failed")
        try:
            publisher(receipt)
        except BaseException:
            with self._lock:
                active = self._active_route_requests.get(request_id)
                if active is not None:
                    active["cleanup_receipt_publication_started"] = False
                    active["cleanup_receipt_publication_event"] = threading.Event()
                publication_event.set()
            raise
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is not None:
                active["cleanup_receipt_published"] = True
            publication_event.set()
        return receipt

    def _schedule_cleanup_reconciliation(
        self,
        *,
        request_id: str,
        cleanup_subject: Mapping[str, Any],
        participating_node_ids: frozenset[str],
    ) -> None:
        """Give late exact cleanup proof one bounded post-authority owner."""

        frozen_subject = dict(cleanup_subject)
        frozen_nodes = frozenset(participating_node_ids)
        now = time.monotonic()
        # This owner begins after the immutable data/cancellation authority has
        # expired.  Its sole capability is observing and publishing exact,
        # generation-fenced cleanup proof; capping it by the earlier data
        # request deadline made the post-authority owner a no-op precisely when
        # a late receipt required it.  Keep the reconciliation horizon bounded
        # from this scheduling point without extending mutation authority.
        final_deadline = now + _CLEANUP_RECONCILIATION_HORIZON_SECONDS
        with self._lock:
            stop = getattr(self, "_cleanup_reconciliation_stop", None)
            if not isinstance(stop, threading.Event):
                stop = threading.Event()
                self._cleanup_reconciliation_stop = stop
            active = self._active_route_requests.get(request_id)
            if (
                active is None
                or active.get("terminal") is True
                or active.get("cleanup_receipt_published") is True
                or getattr(self, "_closed", False)
                or stop.is_set()
            ):
                return
            futures = getattr(self, "_cleanup_reconciliation_futures", None)
            if futures is None:
                futures = {}
                self._cleanup_reconciliation_futures = futures
            prior = futures.get(request_id)
            if prior is not None and not prior.done():
                return
            self._submit_cleanup_reconciliation_locked(
                request_id=request_id,
                cleanup_subject=frozen_subject,
                participating_node_ids=frozen_nodes,
                final_deadline_monotonic_s=final_deadline,
                stop=stop,
            )

    def _submit_cleanup_reconciliation_locked(
        self,
        *,
        request_id: str,
        cleanup_subject: Mapping[str, Any],
        participating_node_ids: frozenset[str],
        final_deadline_monotonic_s: float,
        stop: threading.Event,
        last_error: str = "route_cancellation_cleanup_unproven",
        retry_not_before_monotonic_s: float | None = None,
    ) -> None:
        """Submit one fair proof slice while ``self._lock`` is held."""

        executor = getattr(self, "_cleanup_reconciliation_executor", None)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=_CLEANUP_RECONCILIATION_WORKERS,
                thread_name_prefix="mycelium-cleanup-reconcile",
            )
            self._cleanup_reconciliation_executor = executor
        future = executor.submit(
            self._reconcile_request_cleanup,
            request_id=request_id,
            cleanup_subject=cleanup_subject,
            participating_node_ids=participating_node_ids,
            final_deadline_monotonic_s=final_deadline_monotonic_s,
            stop=stop,
            last_error=last_error,
            retry_not_before_monotonic_s=retry_not_before_monotonic_s,
        )
        self._cleanup_reconciliation_futures[request_id] = future

        def retire(completed: Any) -> None:
            with self._lock:
                current = getattr(self, "_cleanup_reconciliation_futures", {}).get(
                    request_id
                )
                if current is completed:
                    self._cleanup_reconciliation_futures.pop(request_id, None)

        future.add_done_callback(retire)

    def _reconcile_request_cleanup(
        self,
        *,
        request_id: str,
        cleanup_subject: Mapping[str, Any],
        participating_node_ids: frozenset[str],
        final_deadline_monotonic_s: float,
        stop: threading.Event,
        last_error: str = "route_cancellation_cleanup_unproven",
        retry_not_before_monotonic_s: float | None = None,
    ) -> None:
        if stop.is_set():
            return
        # Queue the retry before waiting: a ready sibling already in the
        # bounded executor must run before this owner's next poll interval.
        if retry_not_before_monotonic_s is not None:
            stop.wait(max(0.0, min(
                retry_not_before_monotonic_s,
                final_deadline_monotonic_s,
            ) - time.monotonic()))
        if stop.is_set():
            return
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if (
                active is None
                or active.get("terminal") is True
                or active.get("cleanup_receipt_published") is True
                or getattr(self, "_closed", False)
            ):
                return
        if time.monotonic() < final_deadline_monotonic_s:
            attempt_deadline = min(
                final_deadline_monotonic_s,
                time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS,
            )
            try:
                self._wait_for_cancellation_cleanup(
                    participating_node_ids,
                    deadline_monotonic_s=attempt_deadline,
                    cleanup_subject=cleanup_subject,
                    receipt_only=True,
                )
                receipt = self._seal_and_publish_request_cleanup_receipt(
                    request_id=request_id,
                    cleanup_subject=cleanup_subject,
                    participating_node_ids=participating_node_ids,
                )
                if receipt is not None:
                    with self._lock:
                        active = self._active_route_requests.get(request_id)
                        if active is not None:
                            active.pop("cleanup_reconciliation_error", None)
                    return
            except BaseException as error:
                remote_code = getattr(error, "remote_code", None)
                last_error = str(
                    remote_code
                    if isinstance(remote_code, str) and remote_code
                    else getattr(error, "code", str(error) or type(error).__name__)
                )[:128]
                with self._lock:
                    active = self._active_route_requests.get(request_id)
                    if active is not None:
                        active["cleanup_reconciliation_error"] = last_error

        remaining = final_deadline_monotonic_s - time.monotonic()
        if not stop.is_set() and remaining > 0:
            with self._lock:
                active = self._active_route_requests.get(request_id)
                if (
                    active is None
                    or active.get("terminal") is True
                    or active.get("cleanup_receipt_published") is True
                    or getattr(self, "_closed", False)
                ):
                    return
                try:
                    # A failed owner moves to the tail immediately. Its retry
                    # job waits for the poll cadence only after queued siblings
                    # have had a chance to run within their own deadlines.
                    self._submit_cleanup_reconciliation_locked(
                        request_id=request_id,
                        cleanup_subject=cleanup_subject,
                        participating_node_ids=participating_node_ids,
                        final_deadline_monotonic_s=final_deadline_monotonic_s,
                        stop=stop,
                        last_error=last_error,
                        retry_not_before_monotonic_s=min(
                            final_deadline_monotonic_s,
                            time.monotonic() + _CLEANUP_RECEIPT_POLL_SECONDS,
                        ),
                    )
                except RuntimeError:
                    if stop.is_set() or getattr(self, "_closed", False):
                        return
                    raise
            return

        if stop.is_set():
            return
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if (
                active is None
                or active.get("terminal") is True
                or active.get("cleanup_receipt_published") is True
                or getattr(self, "_closed", False)
            ):
                return
            active["cleanup_reconciliation_error"] = (
                f"cleanup_reconciliation_timeout:{last_error}"[:128]
            )
        self._record_scoped_runtime_incident(
            request_id=request_id,
            subject_id="physical-live-route",
            scope="request",
            reason="cleanup_reconciliation_timeout",
        )

    def _request_control_identity(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None:
                raise RuntimeError("request_control_identity_unavailable")
            return {
                field: active[field]
                for field in (
                    "deployment_id",
                    "deployment_epoch",
                    "qualification_digest",
                    "request_attempt",
                    "path_id",
                    "path_attempt",
                    "path_digest",
                    "topology_generation",
                    "command_id",
                    "cancellation_generation",
                    "publisher_generation",
                    "absolute_deadline_ms",
                )
            }

    def _request_cleanup_subject(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None:
                raise RuntimeError("request_control_identity_unavailable")
            frozen = active.get("cancellation_cleanup_subject")
            if isinstance(frozen, Mapping):
                return dict(frozen)
            return {
                "request_id": request_id,
                **{
                    field: active[field]
                    for field in (
                        "deployment_id",
                        "deployment_epoch",
                        "qualification_digest",
                        "request_attempt",
                        "path_id",
                        "path_attempt",
                        "path_digest",
                        "topology_generation",
                        "command_id",
                        "cancellation_generation",
                        "publisher_generation",
                        "absolute_deadline_ms",
                    )
                },
            }

    def cancel_request(
        self,
        request_id: str,
        *,
        deadline_monotonic_s: float | None = None,
    ) -> bool:
        """Interrupt one physical request without terminating its shared node."""

        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None or active.get("terminal") is True:
                return False
            if active.get("cancellation_requested") is True:
                return False
            active["cancellation_requested"] = True
            active["cancellation_generation"] += 1
            active["cancellation_started_at"] = time.monotonic()
            fanout_complete = threading.Event()
            active["cancellation_fanout_complete"] = fanout_complete
            if deadline_monotonic_s is None:
                deadline_monotonic_s = active["cancellation_started_at"] + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
            if (
                not isinstance(deadline_monotonic_s, (int, float))
                or isinstance(deadline_monotonic_s, bool)
                or deadline_monotonic_s <= active["cancellation_started_at"]
                or deadline_monotonic_s > active["cancellation_started_at"] + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
            ):
                raise ValueError("invalid_cancellation_deadline")
            active["cancellation_deadline"] = float(deadline_monotonic_s)
            participating_node_ids = frozenset(active["participating_node_ids"])
            inflight_commands = dict(active.get("inflight_commands", {}))
            control_bound_event = active.get("control_bound_event")
            if (
                isinstance(control_bound_event, threading.Event)
                and not control_bound_event.is_set()
            ):
                # These are generation-0 bind_request_control commands, not
                # inference work. Do not interrupt them: a bind that loses to
                # cleanup validates the sealed generation-1 receipt and
                # acknowledges without recreating state. The cancellation
                # fanout below must not wait for these ordinary-lane replies;
                # infer_cancel_wait already carries the complete immutable
                # identity and can establish pending control itself.
                inflight_commands = {}
            deadline = float(active["cancellation_deadline"])
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
            payload = {
                "request_id": request_id,
                "request_attempt": active["request_attempt"],
                "deployment_id": active["deployment_id"],
                "deployment_epoch": active["deployment_epoch"],
                "qualification_digest": active["qualification_digest"],
                "command_id": active["command_id"],
                "publisher_generation": active["publisher_generation"],
                "absolute_deadline_ms": active["absolute_deadline_ms"],
                "path_id": active["path_id"],
                "path_attempt": active["path_attempt"],
                "path_digest": active["path_digest"],
                "topology_generation": active["topology_generation"],
                "cancellation_generation": active["cancellation_generation"],
                "deadline_budget_ms": min(mycelium_deadlines.COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS, remaining_ms),
            }
            # Cancellation linearizes one immutable cleanup identity. Gateway
            # publication authority may advance independently while terminal
            # delivery is in flight, but every node fanout, fallback snapshot,
            # and route receipt must prove the exact control generation that
            # was actually cancelled.
            active["cancellation_cleanup_subject"] = {
                key: value
                for key, value in payload.items()
                if key != "deadline_budget_ms"
            }
        # Wake only this request's correlated control waiters. The node process
        # remains shared and alive; any later response is retired by command ID.
        for node_id, command_id in sorted(inflight_commands.items()):
            session = self._sessions.get(node_id)
            interrupt = getattr(session, "interrupt_command", None)
            if callable(interrupt):
                try:
                    interrupt(command_id, code="request_cancelled")
                except BaseException:
                    pass

        # Every participating node owns request/path-scoped runtime or transport
        # state.  Propagate the same owner-issued cancellation generation and
        # absolute deadline to each node; a relay-level path cancellation alone
        # cannot advance the remote node's command-control generation.
        def cancel_node(node_id: str) -> tuple[str, dict[str, Any] | None]:
            # A failed data plane is a reason to cancel, not evidence that
            # cancellation happened. Keep the bounded independent control
            # attempt and require its exact signed cleanup observation.
            # The node deliberately returns its inline cleanup observation
            # before consuming the owner's whole deadline, but the parent must
            # enforce the same partition on response delivery.  Waiting here
            # until the absolute owner deadline leaves no authority for the
            # request-scoped snapshot fallback when a complete response was
            # produced but delayed in the SSH/control transport.  Reserve one
            # existing bounded snapshot attempt; this does not extend the
            # owner cancellation-and-cleanup window (see ``_MAX_CANCELLATION_AND_CLEANUP_SECONDS``).
            now = time.monotonic()
            fanout_deadline = deadline
            if deadline - now > _CLEANUP_FALLBACK_RESERVE_SECONDS:
                fanout_deadline = deadline - _CLEANUP_FALLBACK_RESERVE_SECONDS
            # The node receives a duration, whereas this parent enforces an
            # absolute deadline. Age that duration to the same reserved fanout
            # boundary immediately before the frame is sent. Otherwise the
            # node can keep its inline cleanup owner alive after this waiter is
            # retired, causing it to overlap and starve the fallback snapshot
            # during the final reserved interval.
            node_payload = {
                **payload,
                "deadline_budget_ms": min(
                    payload["deadline_budget_ms"],
                    max(1, int((fanout_deadline - time.monotonic()) * 1_000)),
                ),
            }
            response = self._send_before(
                node_id,
                command_id=self._command_id(node_id, "infer-cancel"),
                command="infer_cancel_wait",
                payload=node_payload,
                deadline_monotonic_s=fanout_deadline,
            )
            observation = self._verify_observation(
                node_id,
                response,
                event="inference_cancelled",
            )
            return node_id, observation

        def fanout() -> None:
            try:
                # Generation-1 fanout is authoritative even when it overtakes
                # generation-0 binding. Nodes record the full immutable
                # control as pending cancellation, and a later bind validates
                # either that pending identity or its sealed cleanup receipt.
                # Waiting for ordinary bind responses here serialized
                # cancellation behind admission and could consume the entire
                # immutable cleanup deadline before any teardown began.
                errors: list[BaseException] = []
                with ThreadPoolExecutor(
                    max_workers=len(participating_node_ids),
                    thread_name_prefix="a4-cancel-fanout",
                ) as executor:
                    futures = {
                        executor.submit(cancel_node, node_id): node_id
                        for node_id in sorted(participating_node_ids)
                    }
                    for future in as_completed(futures):
                        try:
                            node_id, observation = future.result()
                        except BaseException as error:
                            node_id = futures[future]
                            reason = str(
                                getattr(
                                    error,
                                    "code",
                                    str(error) or type(error).__name__,
                                )
                            )
                            self._record_response_reader_failure_once(
                                request_id=request_id,
                                node_id=node_id,
                                reason=reason,
                                observed_at_monotonic_s=time.monotonic(),
                            )
                            errors.append(error)
                            continue
                        details = (
                            observation.get("details")
                            if isinstance(observation, Mapping)
                            else None
                        )
                        receipt = (
                            details.get("request_cleanup")
                            if isinstance(details, Mapping)
                            else None
                        )
                        if not isinstance(receipt, Mapping):
                            continue
                        with self._lock:
                            # ``infer_cancel_wait`` returns a deliberately
                            # narrow receipt observation.  It does not carry
                            # the runtime identity/capability fields present
                            # in a full node snapshot, so publishing it as the
                            # node's latest general snapshot makes stable live
                            # route fields (notably decode_mode) disappear or
                            # reappear according to cleanup race ordering.
                            # Keep cleanup proof in its request-scoped ledger;
                            # only _snapshot_nodes[_before] may replace the
                            # full peer projection.
                            scoped_by_request = getattr(
                                self, "_cleanup_snapshots_by_request", None
                            )
                            if scoped_by_request is None:
                                scoped_by_request = {}
                                self._cleanup_snapshots_by_request = scoped_by_request
                            request_snapshots = scoped_by_request.setdefault(
                                request_id, {}
                            )
                            existing = request_snapshots.get(node_id)
                            existing_details = (
                                existing.get("details")
                                if isinstance(existing, Mapping)
                                else None
                            )
                            existing_receipt = (
                                existing_details.get("request_cleanup")
                                if isinstance(existing_details, Mapping)
                                else None
                            )
                            # Preserve every verified owner observation for
                            # exact blocker diagnostics, but never move a
                            # request/node ledger backwards after complete
                            # cleanup has been proved by either the inline
                            # command or a racing fallback snapshot.
                            if (
                                not isinstance(existing_receipt, Mapping)
                                or existing_receipt.get("complete") is not True
                                or receipt.get("complete") is True
                            ):
                                request_snapshots[node_id] = observation
                # If every leaf returned an exact receipt, seal and publish the
                # route aggregate before releasing the fanout barrier.  The
                # inference worker may be concurrently unwinding after its
                # interrupted data command; it is a waiter on this lifecycle,
                # not the sole owner of command-cleanup publication.
                self._seal_and_publish_request_cleanup_receipt(
                    request_id=request_id,
                    cleanup_subject={
                        key: value
                        for key, value in payload.items()
                        if key != "deadline_budget_ms"
                    },
                    participating_node_ids=participating_node_ids,
                )
                if errors:
                    raise errors[0]
            except BaseException as error:
                remote_code = getattr(error, "remote_code", None)
                code = str(
                    remote_code
                    if isinstance(remote_code, str) and remote_code
                    else getattr(error, "code", str(error) or type(error).__name__)
                )
                with self._lock:
                    current = self._active_route_requests.get(request_id)
                    if current is not None:
                        current["cancellation_fanout_error"] = code
            finally:
                # A lost peer may reject its fanout leaf while surviving peers
                # still consume the same generation-fenced interruption.  The
                # event tracks acknowledgement completion for diagnostics;
                # exact per-node cleanup receipts remain authoritative.
                fanout_complete.set()
                self._schedule_cleanup_reconciliation(
                    request_id=request_id,
                    cleanup_subject={
                        key: value
                        for key, value in payload.items()
                        if key != "deadline_budget_ms"
                    },
                    participating_node_ids=participating_node_ids,
                )

        fanout_thread = threading.Thread(
            target=fanout,
            name=f"a4-cancel-fanout-owner-{request_id[:12]}",
            daemon=True,
        )
        with self._lock:
            current = self._active_route_requests.get(request_id)
            if current is None:
                return False
            current["cancellation_fanout_thread"] = fanout_thread
        try:
            fanout_thread.start()
        except BaseException:
            fanout_complete.set()
            raise
        return True

    def _install_observed_owner_cancellation(
        self,
        request_id: str,
        *,
        authorize_cleanup: Callable[[float], float],
    ) -> float:
        """Install a gateway cancellation observed after route registration.

        The gateway command controller can linearize cancellation before this
        route has registered ``request_id``.  Its first ``cancel_request`` call
        then correctly returns false, but the request callback remains the
        durable handoff of that already-authorized command generation.  Once
        registration is visible, install the same owner deadline on the
        physical route before any inference command can make progress.
        """

        observed_route_deadline: float | None = None
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None or active.get("terminal") is True:
                raise RuntimeError("route_cancellation_rejected")
            if active.get("cancellation_requested") is True:
                deadline = active.get("cancellation_deadline")
                if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
                    raise RuntimeError("route_cancellation_deadline_missing")
                observed_route_deadline = float(deadline)

        if observed_route_deadline is not None:
            # Route liveness can start generation-fenced physical teardown
            # before the inference owner observes the failure.  That safety
            # action does not itself advance the gateway command controller.
            # Re-enter its idempotent authorization callback before accepting
            # or publishing the route's generation-1 cleanup receipt.  If the
            # gateway already owns the generation this returns its existing
            # deadline without minting another one; otherwise it closes the
            # controller/physical ordering gap exposed by sustained traffic.
            return float(authorize_cleanup(observed_route_deadline))

        cancellation_deadline = authorize_cleanup(time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS)
        if not self.cancel_request(
            request_id,
            deadline_monotonic_s=cancellation_deadline,
        ):
            # A concurrent gateway cancellation may have installed the exact
            # generation between the read above and cancel_request().
            with self._lock:
                active = self._active_route_requests.get(request_id)
                deadline = (
                    None
                    if active is None
                    or active.get("cancellation_requested") is not True
                    else active.get("cancellation_deadline")
                )
            if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
                raise RuntimeError("route_cancellation_rejected")
            return float(deadline)
        return float(cancellation_deadline)

    def _authorize_failed_request_cleanup(
        self,
        request_id: str,
        *,
        failure_observed_at: float,
        authorize_cleanup: Callable[[float], float],
    ) -> float:
        """Join route-first teardown to the gateway command generation.

        Active liveness may linearize physical generation-1 cancellation before
        the inference worker observes the node failure.  The worker must still
        re-enter the gateway's idempotent authorization callback before it can
        publish that generation-1 cleanup receipt.  Merely reusing the route's
        deadline leaves the command controller at generation 0 and makes exact
        cleanup proof fail closed with an identity mismatch.
        """

        with self._lock:
            active = self._active_route_requests.get(request_id)
            existing_deadline = (
                None if active is None else active.get("cancellation_deadline")
            )
            cancellation_requested = bool(
                active is not None and active.get("cancellation_requested") is True
            )
        if cancellation_requested:
            return self._install_observed_owner_cancellation(
                request_id,
                authorize_cleanup=authorize_cleanup,
            )

        proposed_deadline = (
            float(existing_deadline)
            if existing_deadline is not None
            else failure_observed_at + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
        )
        cleanup_deadline = authorize_cleanup(proposed_deadline)
        if self.cancel_request(
            request_id,
            deadline_monotonic_s=cleanup_deadline,
        ):
            return float(cleanup_deadline)
        # Route liveness may win between the state read and cancel_request().
        # Join that exact generation/deadline instead of publishing a receipt
        # for a controller generation that was never advanced.
        return self._install_observed_owner_cancellation(
            request_id,
            authorize_cleanup=authorize_cleanup,
        )

    def update_publisher_generation(
        self,
        request_id: str,
        *,
        expected_generation: int,
        new_generation: int,
        route_identity: Mapping[str, Any],
    ) -> bool:
        """CAS a gateway-owned publisher generation onto one physical request."""

        if (
            type(expected_generation) is not int
            or expected_generation < 1
            or type(new_generation) is not int
            or new_generation != expected_generation + 1
        ):
            return False
        identity = dict(route_identity)
        expected_fields = {
            "request_id",
            "request_attempt",
            "path_id",
            "path_attempt",
            "path_manifest_digest",
            "deployment_id",
            "deployment_epoch",
            "topology_generation",
        }
        if set(identity) != expected_fields or identity["request_id"] != request_id:
            return False
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None:
                prior = self._pending_publisher_generations.get(request_id)
                candidate = (expected_generation, new_generation, identity)
                if prior is not None and prior != candidate:
                    return False
                self._pending_publisher_generations[request_id] = candidate
                return True
            if (
                active["request_attempt"] != identity["request_attempt"]
                or active["path_id"] != identity["path_id"]
                or active["path_attempt"] != identity["path_attempt"]
                or active["path_digest"] != identity["path_manifest_digest"]
                or active["deployment_id"] != identity["deployment_id"]
                or active["deployment_epoch"] != identity["deployment_epoch"]
                or active["topology_generation"] != identity["topology_generation"]
                or active["publisher_generation"] != expected_generation
                or active.get("terminal") is True
            ):
                return False
            active["publisher_generation"] = new_generation
            control_bound = active.get("control_bound") is True
            participating = frozenset(active["participating_node_ids"])
        if control_bound:
            self._propagate_publisher_generation(request_id, participating)
        return True

    def _propagate_publisher_generation(
        self,
        request_id: str,
        participating_node_ids: frozenset[str],
    ) -> None:
        control = self._request_control_identity(request_id)
        for node_id in sorted(participating_node_ids):
            response = self._sessions[node_id].send(
                command_id=self._command_id(node_id, "update-request-control"),
                command="update_request_control",
                payload={"request_id": request_id, "control": control},
            )
            self._verify_observation(
                node_id,
                response,
                event="request_control_updated",
            )

    def infer_startup_challenge(
        self,
        *,
        request_id: str,
        sink: TokenSink,
    ) -> InferenceResult:
        """Execute the immutable qualification challenge without UI filtering.

        Product stop tokens are configured after the first physical startup
        qualification.  A later authority renewal must reproduce the exact
        controller-owned output, including any token that is terminal for the
        browser codec.  Applying product filtering here truncates that output
        and incorrectly turns a successful challenge into cancellation cleanup.

        Keep the override request-local.  Temporarily mutating the route-wide
        stop set would let an already-authorized product request race renewal
        and expose a normally filtered terminal token.
        """

        startup_prompt, startup_output = self.startup_challenge
        return self.infer(
            startup_prompt,
            max_new_tokens=len(startup_output),
            request_id=request_id,
            sink=sink,
            _stop_token_ids_override=frozenset(),
        )

    def infer(
        self,
        token_ids: Sequence[int],
        *,
        max_new_tokens: int,
        request_id: str,
        sink: TokenSink,
        cancel_requested: Callable[[], bool] | None = None,
        selected_placement_ids: Sequence[str] | None = None,
        route_identity: Mapping[str, Any] | None = None,
        locked_path_manifest: Mapping[str, Any] | None = None,
        command_identity: Mapping[str, Any] | None = None,
        authorize_cleanup: Callable[[float], float] | None = None,
        publish_cleanup_receipt: Callable[[Mapping[str, Any]], None] | None = None,
        _stop_token_ids_override: frozenset[int] | None = None,
        speculation_preference: str | None = None,
    ) -> InferenceResult:
        # This lock is request-scoped. No route-global mutex spans physical
        # prefill, decode, cancellation, cleanup, or browser streaming.
        # The context manager also opens this request's hop trace, so a live run
        # emits hop telemetry with no external wrapping (see _instrumented_request).
        with self._instrumented_request(request_id):
            if (
                getattr(self, "_closed", False)
                or not getattr(self, "_open", True)
                or self._fatal is not None
            ):
                raise RuntimeError("route_not_open")
            if (
                not isinstance(max_new_tokens, int)
                or isinstance(max_new_tokens, bool)
                or max_new_tokens < 1
                or not isinstance(request_id, str)
                or not request_id
            ):
                raise ValueError("invalid_inference_request")
            if _stop_token_ids_override is not None and (
                not isinstance(_stop_token_ids_override, frozenset)
                or any(
                    not isinstance(token_id, int)
                    or isinstance(token_id, bool)
                    or token_id < 0
                    for token_id in _stop_token_ids_override
                )
            ):
                raise ValueError("invalid_stop_token_ids_override")
            # A11: an explicit future-request preference only.  The route never
            # infers or defaults it; the entry node admits or refuses it against
            # its staged draft binding and measured baseline, and any refusal is
            # an ordinary target-only request.  Recoveries (A6 replay, A7
            # successor) re-enter infer without it and run target-only.
            if speculation_preference not in (None, "target_only", "speculative_requested"):
                raise ValueError("invalid_speculation_preference")
            request_stop_token_ids = (
                self._stop_token_ids
                if _stop_token_ids_override is None
                else _stop_token_ids_override
            )
            graph = getattr(self, "_graph", None)
            locked_manifest = None
            if route_identity is not None:
                if selected_placement_ids is not None:
                    raise ValueError("locked_path_cannot_select_placements")
                if not isinstance(locked_path_manifest, Mapping):
                    raise ValueError("locked_path_manifest_required")
                if graph is None:
                    raise ValueError("locked_path_graph_unavailable")
                from mycelium_router.serialization import path_manifest_from_dict
                from mycelium_router.validation import validate_manifest

                locked_manifest = path_manifest_from_dict(dict(locked_path_manifest))
                validate_manifest(locked_manifest, graph)
            elif locked_path_manifest is not None:
                raise ValueError("unexpected_locked_path_manifest")
            if graph is None:
                explicit_selection = False
                if selected_placement_ids is not None:
                    raise ValueError("invalid_selected_placement_ids")
                entry_node_id = self._plan["entry_node_id"]
                excluded_placement_ids: list[str] = []
                participating_node_ids = frozenset(self._sessions)
            else:
                explicit_selection = (
                    selected_placement_ids is not None or locked_manifest is not None
                )
                selected = (
                    tuple(hop.placement_id for hop in locked_manifest.ordered_hops)
                    if locked_manifest is not None
                    else tuple(selected_placement_ids)
                    if selected_placement_ids is not None
                    else tuple(
                        stage.placements[0].placement_id for stage in graph.stages
                    )
                )
                if len(selected) != len(graph.stages) or len(set(selected)) != len(
                    selected
                ):
                    raise ValueError("invalid_selected_placement_ids")
                selected_placements = []
                for stage, placement_id in zip(graph.stages, selected, strict=True):
                    matches = [
                        placement
                        for placement in stage.placements
                        if placement.placement_id == placement_id
                    ]
                    if len(matches) != 1:
                        raise ValueError("invalid_selected_placement_ids")
                    selected_placements.append(matches[0])
                entry_node_id = selected_placements[0].node_id
                self._require_stage_only_positions([p.node_id for p in selected_placements])
                participating_node_ids = frozenset(
                    placement.node_id for placement in selected_placements
                )
                excluded_placement_ids = (
                    []
                    if locked_manifest is not None
                    else [
                        placement.placement_id
                        for stage in graph.stages
                        for placement in stage.placements
                        if placement.placement_id not in selected
                    ]
                )
            if any(
                getattr(self._sessions[node_id], "returncode", None) is not None
                for node_id in participating_node_ids
            ):
                raise RuntimeError("selected_route_not_open")
            self._reject_if_affected_peer_quarantined(participating_node_ids)
            # Metrics collection is not part of request identity or cleanup
            # proof.  Defer it until after the request is registered so an
            # owner cancellation cannot be stranded behind a pre-registration
            # physical snapshot with no route generation to interrupt.
            before_peers: dict[str, dict[str, int]] | None = None
            started_at = time.monotonic()
            prefill_completed_at: float | None = None
            token_times: list[float] = []
            request = {
                **self._plan["request"],
                "request_id": request_id,
                "prompt_token_ids": list(token_ids),
                "max_new_tokens": max_new_tokens,
                "expected_new_tokens": max_new_tokens,
            }
            if route_identity is None:
                # Legacy qualification helpers do not use the product coordinator.
                # Product requests are required to pass the M16-owned identity.
                path_identity = {
                    "request_id": request_id,
                    "request_attempt": 1,
                    "path_id": f"legacy:{request_id}",
                    "path_attempt": 0,
                    "path_manifest_digest": "sha256:"
                    + hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
                    "deployment_id": (
                        graph.deployment_id
                        if graph is not None
                        else self._plan.get("deployment_id", "legacy-route")
                    ),
                    "deployment_epoch": graph.deployment_epoch
                    if graph is not None
                    else 0,
                    "topology_generation": (
                        graph.topology_version if graph is not None else 0
                    ),
                }
            else:
                path_identity = dict(route_identity)
                expected = {
                    "request_id",
                    "request_attempt",
                    "path_id",
                    "path_attempt",
                    "path_manifest_digest",
                    "deployment_id",
                    "deployment_epoch",
                    "topology_generation",
                }
                if (
                    set(path_identity) != expected
                    or path_identity["request_id"] != request_id
                ):
                    raise ValueError("invalid_route_identity")
                if graph is not None and (
                    path_identity["deployment_id"] != graph.deployment_id
                    or path_identity["deployment_epoch"] != graph.deployment_epoch
                    or path_identity["topology_generation"] != graph.topology_version
                ):
                    raise ValueError("stale_route_identity")
            if route_identity is not None:
                assert locked_manifest is not None
                assert locked_path_manifest is not None
                serialized_digest = (
                    "sha256:"
                    + hashlib.sha256(
                        json.dumps(
                            dict(locked_path_manifest),
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                )
                if (
                    locked_manifest.request_id != request_id
                    or locked_manifest.path_id != path_identity["path_id"]
                    or locked_manifest.path_attempt != path_identity["path_attempt"]
                    or serialized_digest != path_identity["path_manifest_digest"]
                ):
                    raise ValueError("locked_path_identity_mismatch")
            path_digest = str(path_identity["path_manifest_digest"])
            qualification = getattr(self, "_deployment_qualification", None)
            if route_identity is not None:
                if (
                    qualification is None
                    or getattr(qualification, "route_ready", False) is not True
                ):
                    raise RuntimeError("deployment_qualification_unavailable")
                from mycelium_request_gateway.contracts import qualification_digest

                current_qualification_digest = qualification_digest(qualification)
            else:
                current_qualification_digest = (
                    "sha256:"
                    + hashlib.sha256(b"legacy-qualification-helper").hexdigest()
                )
            if route_identity is not None:
                expected_command_fields = {
                    "deployment_id",
                    "deployment_epoch",
                    "qualification_digest",
                    "request_id",
                    "request_attempt",
                    "path_id",
                    "path_attempt",
                    "path_digest",
                    "topology_generation",
                    "command_id",
                    "publisher_generation",
                    "absolute_deadline_ms",
                    "cancellation_generation",
                }
                if not isinstance(command_identity, Mapping):
                    raise ValueError("command_identity_required")
                command_control = dict(command_identity)
                if (
                    set(command_control) != expected_command_fields
                    or command_control["request_id"] != request_id
                    or command_control["deployment_id"]
                    != path_identity["deployment_id"]
                    or command_control["deployment_epoch"]
                    != path_identity["deployment_epoch"]
                    or command_control["qualification_digest"]
                    != current_qualification_digest
                    or command_control["request_attempt"]
                    != path_identity["request_attempt"]
                    or command_control["path_id"] != path_identity["path_id"]
                    or command_control["path_attempt"] != path_identity["path_attempt"]
                    or command_control["path_digest"] != path_digest
                    or command_control["topology_generation"]
                    != path_identity["topology_generation"]
                    or not isinstance(command_control["command_id"], str)
                    or not command_control["command_id"]
                    or type(command_control["publisher_generation"]) is not int
                    or command_control["publisher_generation"] < 1
                    or type(command_control["absolute_deadline_ms"]) is not int
                    or command_control["absolute_deadline_ms"]
                    <= int(time.monotonic() * 1_000)
                    or command_control["cancellation_generation"] != 0
                ):
                    raise ValueError("invalid_command_identity")
                with self._lock:
                    pending_publisher = self._pending_publisher_generations.pop(
                        request_id,
                        None,
                    )
                if pending_publisher is not None:
                    expected_publisher, new_publisher, pending_identity = (
                        pending_publisher
                    )
                    if (
                        pending_identity != path_identity
                        or command_control["publisher_generation"] != expected_publisher
                        or new_publisher != expected_publisher + 1
                    ):
                        raise ValueError("stale_publisher_generation")
                    command_control["publisher_generation"] = new_publisher
            elif command_identity is not None:
                raise ValueError("unexpected_command_identity")
            if route_identity is not None and authorize_cleanup is None:
                raise ValueError("cleanup_authority_required")
            if route_identity is None and authorize_cleanup is not None:
                raise ValueError("unexpected_cleanup_authority")
            if route_identity is not None and publish_cleanup_receipt is None:
                raise ValueError("cleanup_receipt_publisher_required")
            if route_identity is None and publish_cleanup_receipt is not None:
                raise ValueError("unexpected_cleanup_receipt_publisher")
            with self._lock:
                active_requests = getattr(self, "_active_route_requests", None)
                if active_requests is None:
                    active_requests = {}
                    self._active_route_requests = active_requests
                self._active_route_requests[request_id] = {
                    "request_attempt": path_identity["request_attempt"],
                    "path_id": path_identity["path_id"],
                    "path_attempt": path_identity["path_attempt"],
                    "path_digest": path_digest,
                    "deployment_id": path_identity["deployment_id"],
                    "deployment_epoch": path_identity["deployment_epoch"],
                    "qualification_digest": current_qualification_digest,
                    "topology_generation": path_identity["topology_generation"],
                    "command_id": (
                        command_control["command_id"]
                        if route_identity is not None
                        else f"legacy:{request_id}"
                    ),
                    "publisher_generation": (
                        command_control["publisher_generation"]
                        if route_identity is not None
                        else 1
                    ),
                    "absolute_deadline_ms": (
                        command_control["absolute_deadline_ms"]
                        if route_identity is not None
                        else int(time.monotonic() * 1_000) + 3_600_000
                    ),
                    "entry_node_id": entry_node_id,
                    "participating_node_ids": participating_node_ids,
                    "cancellation_generation": 0,
                    "cancellation_requested": False,
                    "cancellation_started_at": None,
                    "cancellation_deadline": None,
                    "cancellation_fanout_complete": None,
                    "terminal": False,
                    "control_bound": False,
                    "control_bound_event": (
                        threading.Event() if route_identity is not None else None
                    ),
                    "publish_cleanup_receipt": publish_cleanup_receipt,
                    "cleanup_receipt_publication_event": threading.Event(),
                    "cleanup_receipt_publication_started": False,
                    "cleanup_receipt_published": False,
                }
            cleanup_subject = self._request_cleanup_subject(request_id)
            try:
                cancelled_before_start = False
                if route_identity is not None:
                    bind_control = self._request_control_identity(request_id)
                    for node_id in sorted(participating_node_ids):
                        bound_response = self._send_request_command(
                            request_id=request_id,
                            node_id=node_id,
                            command_id=self._command_id(
                                node_id,
                                "bind-request-control",
                            ),
                            command="bind_request_control",
                            payload={
                                "request_id": request_id,
                                "control": bind_control,
                            },
                        )
                        self._verify_observation(
                            node_id,
                            bound_response,
                            event="request_control_bound",
                        )
                    with self._lock:
                        active = self._active_route_requests[request_id]
                        active["control_bound"] = True
                        control_bound_event = active.get("control_bound_event")
                        publisher_changed_during_bind = (
                            active["publisher_generation"]
                            != bind_control["publisher_generation"]
                        )
                    if publisher_changed_during_bind:
                        self._propagate_publisher_generation(
                            request_id,
                            participating_node_ids,
                        )
                    if isinstance(control_bound_event, threading.Event):
                        control_bound_event.set()
                    # Cancellation may have linearized at the gateway before
                    # this request was present in _active_route_requests.  The
                    # callback is the exact post-registration handoff: install
                    # its already-authorized owner generation before infer_start.
                    if cancel_requested is not None and cancel_requested():
                        assert authorize_cleanup is not None
                        self._install_observed_owner_cancellation(
                            request_id,
                            authorize_cleanup=authorize_cleanup,
                        )
                        cancelled_before_start = True
                    else:
                        before_peers = self._peer_counters()
                else:
                    # Legacy startup qualification has no product command
                    # identity or owner-cancellation handoff.
                    before_peers = self._peer_counters()
                start_payload: dict[str, Any] = {"request": request}
                if route_identity is not None:
                    start_payload["control"] = {
                        "request_attempt": path_identity["request_attempt"],
                        "path_id": path_identity["path_id"],
                        "path_attempt": path_identity["path_attempt"],
                        "path_digest": path_digest,
                        "deployment_id": path_identity["deployment_id"],
                        "deployment_epoch": path_identity["deployment_epoch"],
                        "qualification_digest": current_qualification_digest,
                        "topology_generation": path_identity["topology_generation"],
                        "cancellation_generation": 0,
                        "command_id": command_control["command_id"],
                        "publisher_generation": command_control["publisher_generation"],
                        "absolute_deadline_ms": command_control["absolute_deadline_ms"],
                    }
                if locked_path_manifest is not None:
                    start_payload["path_manifest"] = dict(locked_path_manifest)
                if excluded_placement_ids:
                    start_payload["excluded_placement_ids"] = excluded_placement_ids
                if speculation_preference is not None:
                    start_payload["speculation"] = {"preference": speculation_preference}
                if cancelled_before_start:
                    # Bind generation 0 on every participant, then let the
                    # already-authorized generation-1 infer_cancel_wait fanout
                    # prove cleanup.  Issuing infer_start after that fanout can
                    # only recreate state behind a sealed cleanup snapshot and
                    # leaves its ordinary response ordered behind the cleanup
                    # receipt reservation.  No inference command is owed once
                    # owner cancellation won before the start boundary.
                    prefill_completed_at = time.monotonic()
                    output = ()
                    status = "CANCELLED"
                else:
                    started_response = self._send_request_command(
                        request_id=request_id,
                        node_id=entry_node_id,
                        command_id=self._command_id(entry_node_id, "infer-start"),
                        command="infer_start",
                        payload=start_payload,
                    )
                    observation = self._verify_observation(
                        entry_node_id,
                        started_response,
                        event="inference_started",
                    )
                    prefill_completed_at = time.monotonic()
                    output = self._output_tokens(observation)
                    status = observation["details"].get("status")
                    # ``inference_started`` is success-shaped.  Only a request
                    # that is decoding, already completed, or owner-cancelled
                    # may continue; a FAILED (or any other) status executed no
                    # usable work and must never surface as an empty
                    # completion.
                    if status not in {"DECODING", "COMPLETED", "CANCELLED"}:
                        raise RuntimeError("route_inference_start_failed")
                emitted = 0
                stopped = False

                def truncate_at_stop(tokens: tuple[int, ...]) -> tuple[int, ...]:
                    nonlocal stopped
                    for index, token_id in enumerate(tokens):
                        if token_id in request_stop_token_ids:
                            stopped = True
                            return tokens[:index]
                    return tokens

                output = truncate_at_stop(output)
                cancelled_for_request = _request_is_cancelled(
                    status,
                    cancel_requested,
                )

                def emit_new_tokens(tokens: tuple[int, ...]) -> None:
                    nonlocal emitted
                    for index in range(emitted, len(tokens)):
                        sink.emit(index, tokens[index])
                        token_times.append(time.monotonic())
                    emitted = len(tokens)

                if not cancelled_for_request:
                    emit_new_tokens(output)
                while (
                    len(output) < max_new_tokens
                    and status == "DECODING"
                    and not stopped
                    and not cancelled_for_request
                ):
                    decode_payload: dict[str, Any] = {
                        "request_id": request_id,
                        "count": 1,
                    }
                    if route_identity is not None:
                        decode_payload["control"] = self._request_control_identity(
                            request_id
                        )
                    decoded_response = self._send_request_command(
                        request_id=request_id,
                        node_id=entry_node_id,
                        command_id=self._command_id(entry_node_id, "infer-decode"),
                        command="infer_decode",
                        payload=decode_payload,
                    )
                    decoded = self._verify_observation(
                        entry_node_id,
                        decoded_response,
                        event="inference_decoded",
                    )
                    next_output = self._output_tokens(decoded)
                    status = decoded["details"].get("status")
                    cancelled_for_request = _request_is_cancelled(
                        status,
                        cancel_requested,
                    )
                    if status == "CANCELLED":
                        break
                    if len(next_output) <= len(output):
                        if status == "COMPLETED":
                            break
                        raise RuntimeError("route_decode_stalled")
                    output = truncate_at_stop(next_output)
                    if not cancelled_for_request:
                        emit_new_tokens(output)
                cancelled_for_stop = stopped and status == "DECODING"
                # A7: an exited participant whose placement a fenced successor
                # replaced is no longer bound to this request (see
                # _bind_covered_peer_exit); its process exit is its proof.
                participating_node_ids = self._bound_participants(
                    request_id, participating_node_ids
                )
                stop_cleanup_authorized = False
                if cancelled_for_stop:
                    if route_identity is None:
                        cancelled_response = self._sessions[entry_node_id].send(
                            command_id=self._command_id(entry_node_id, "infer-cancel"),
                            command="infer_cancel",
                            payload={"request_id": request_id},
                        )
                        self._verify_observation(
                            entry_node_id,
                            cancelled_response,
                            event="inference_cancelled",
                        )
                    else:
                        # A stop token completes the request, so its teardown
                        # is completion cleanup. Take the owner's cleanup
                        # authority first, as the token-bound completion below
                        # does, so the owner's command generation advances
                        # before any node tears down under the next one.
                        # Tearing down first let node receipts reach the owner
                        # while its command still held the previous generation
                        # (identity mismatch) or before its authority time
                        # (cleanup_before_authority); the request then ended
                        # through the cancelled branch although nobody
                        # cancelled it (A6 case 15 on c0dc4350).
                        stop_cleanup_deadline = (
                            time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
                        )
                        if authorize_cleanup is not None:
                            stop_cleanup_deadline = authorize_cleanup(
                                stop_cleanup_deadline
                            )
                            stop_cleanup_authorized = True
                        if not self.cancel_request(
                            request_id,
                            deadline_monotonic_s=stop_cleanup_deadline,
                        ):
                            raise RuntimeError("route_cancellation_rejected")
                output = output[:max_new_tokens]
                if cancelled_for_stop or cancelled_for_request:
                    if cancelled_for_request:
                        assert authorize_cleanup is not None
                        cancellation_deadline = (
                            self._install_observed_owner_cancellation(
                                request_id,
                                authorize_cleanup=authorize_cleanup,
                            )
                        )
                    else:
                        with self._lock:
                            cancellation_deadline = self._active_route_requests[
                                request_id
                            ].get("cancellation_deadline")
                    if cancellation_deadline is None:
                        cancellation_deadline = time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
                    if authorize_cleanup is not None and not stop_cleanup_authorized:
                        cancellation_deadline = authorize_cleanup(
                            float(cancellation_deadline)
                        )
                    cleanup_subject = self._request_cleanup_subject(request_id)
                    self._wait_for_cancellation_cleanup(
                        participating_node_ids,
                        deadline_monotonic_s=float(cancellation_deadline),
                        cleanup_subject=cleanup_subject,
                        receipt_only=True,
                    )
                elif route_identity is not None:
                    # A completed request still owns Router path registration,
                    # stage-local KV, transport forwarding state, and capacity
                    # reservations until an exact generation-fenced teardown is
                    # issued. Observing snapshots cannot cause that cleanup. Use
                    # the same request-scoped cooperative interrupt operation as
                    # cancellation, but retain the already-determined completed
                    # terminal outcome after cleanup is proven.
                    cleanup_deadline = time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
                    assert authorize_cleanup is not None
                    cleanup_deadline = authorize_cleanup(cleanup_deadline)
                    if not self.cancel_request(
                        request_id,
                        deadline_monotonic_s=cleanup_deadline,
                    ):
                        raise RuntimeError("route_completion_cleanup_rejected")
                    cleanup_subject = self._request_cleanup_subject(request_id)
                    self._wait_for_cancellation_cleanup(
                        participating_node_ids,
                        deadline_monotonic_s=cleanup_deadline,
                        cleanup_subject=cleanup_subject,
                        receipt_only=True,
                    )
                elif explicit_selection:
                    self._snapshot_nodes(participating_node_ids)
                else:
                    self._snapshot_all()
                if route_identity is not None:
                    cleanup_receipt = self._seal_and_publish_request_cleanup_receipt(
                        request_id=request_id,
                        cleanup_subject=cleanup_subject,
                        participating_node_ids=participating_node_ids,
                    )
                    if cleanup_receipt is None:
                        raise RuntimeError("route_cleanup_receipt_unsealed")
                if route_identity is not None and not cancelled_for_request:
                    # Governed cleanup is receipt-only and deliberately leaves
                    # the full live-status projection untouched, so without a
                    # refresh peers[] keeps its startup counters forever.  Now
                    # that the cleanup receipt is sealed (outside the strict
                    # cleanup deadline), take ONE full snapshot of the
                    # participants, mirroring live_attestation.  Node RPCs run
                    # without route._lock and _snapshot_nodes commits every
                    # node's observation in one locked update, or none.  The
                    # request already completed: a failed refresh keeps the
                    # previous projection and is recorded, never re-labels the
                    # request as failed.
                    try:
                        self._snapshot_nodes(participating_node_ids)
                    except Exception as refresh_error:
                        self._record_incident(
                            state="post_request_snapshot_refresh_failed",
                            reason=str(
                                getattr(
                                    refresh_error,
                                    "code",
                                    str(refresh_error)
                                    or type(refresh_error).__name__,
                                )
                            ),
                            request_id=request_id,
                        )
                if self._fatal is not None:
                    raise RuntimeError(self._fatal)
                if cancelled_for_request:
                    raise InferenceCancelled("inference_cancelled")
            except BaseException as exc:
                failure_observed_at = time.monotonic()
                remote_reason = getattr(exc, "remote_code", None)
                reason = str(
                    remote_reason
                    if isinstance(remote_reason, str) and remote_reason
                    else getattr(exc, "code", str(exc) or type(exc).__name__)
                )
                with self._lock:
                    active_request = self._active_route_requests.get(request_id)
                    cancellation_requested = bool(
                        active_request is not None
                        and active_request.get("cancellation_requested") is True
                    )
                    cleanup_already_proven = (
                        request_id in self._request_cleanup_receipts
                    )
                    lost_peer_node_ids = frozenset(
                        ()
                        if active_request is None
                        else active_request.get("lost_peer_node_ids", ())
                    )
                # The route's own lost-peer detection (a status read or the
                # liveness monitor) can linearize this cancellation before the
                # exited peer's response reader delivers EOF; the in-flight
                # command is then interrupted as ``request_cancelled``.  That
                # is the participant's process exit, not an owner
                # cancellation: record it exactly as the EOF ordering does, so
                # the request-scoped ``node_process_exited`` incident (the
                # dead peer's cleanup proof and the A6 source-failure fact)
                # exists in both orderings.
                lost_peer_exit_ids = (
                    self._exited_participant_ids(lost_peer_node_ids)
                    if (
                        cancellation_requested
                        and reason == "request_cancelled"
                        and not cleanup_already_proven
                    )
                    else frozenset()
                )
                if cancellation_requested and not lost_peer_exit_ids and (
                    reason == "request_cancelled" or cleanup_already_proven
                ):
                    # The cooperative interrupt retired this request's inflight
                    # node command after owner cancellation, or a late node
                    # error arrived after the exact cleanup receipt had already
                    # been stored and published.  In both orderings the
                    # cancellation CAS plus physical receipt owns the terminal;
                    # a retired inference response cannot reopen it as a
                    # runtime failure.
                    exc = InferenceCancelled("inference_cancelled")
                if lost_peer_exit_ids:
                    for lost_node_id in sorted(lost_peer_exit_ids):
                        self._record_scoped_runtime_incident(
                            request_id=request_id,
                            subject_id=lost_node_id,
                            scope="request",
                            reason="node_process_exited",
                        )
                    reason = "node_process_exited"
                elif not isinstance(exc, InferenceCancelled):
                    if not self._record_response_reader_failure_once(
                        request_id=request_id,
                        node_id=entry_node_id,
                        reason=reason,
                        observed_at_monotonic_s=failure_observed_at,
                    ):
                        self._record_active_runtime_failure(
                            request_id=request_id,
                            node_id=entry_node_id,
                            reason=reason,
                            observed_at_monotonic_s=failure_observed_at,
                        )
                if (
                    route_identity is not None
                    and self.request_cleanup_receipt(request_id) is None
                ):
                    cleanup_deadline = failure_observed_at + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
                    participating_node_ids = self._bound_participants(
                        request_id, participating_node_ids
                    )
                    try:
                        assert authorize_cleanup is not None
                        cleanup_deadline = self._authorize_failed_request_cleanup(
                            request_id,
                            failure_observed_at=failure_observed_at,
                            authorize_cleanup=authorize_cleanup,
                        )
                    except BaseException:
                        # A failed interrupt command is not cleanup evidence.
                        # Exact per-node snapshots below remain authoritative.
                        pass
                    cleanup_subject = self._request_cleanup_subject(request_id)
                    try:
                        self._wait_for_cancellation_cleanup(
                            participating_node_ids,
                            deadline_monotonic_s=cleanup_deadline,
                            cleanup_subject=cleanup_subject,
                            receipt_only=True,
                        )
                    except BaseException as cleanup_error:
                        self._record_incident(
                            state="request_cleanup_unproven",
                            reason=str(
                                getattr(
                                    cleanup_error,
                                    "code",
                                    str(cleanup_error) or type(cleanup_error).__name__,
                                )
                            ),
                            request_id=request_id,
                        )
                        raise RuntimeError(
                            "request_terminal_blocked_cleanup_unproven"
                        ) from exc
                    cleanup_receipt = self._seal_and_publish_request_cleanup_receipt(
                        request_id=request_id,
                        cleanup_subject=cleanup_subject,
                        participating_node_ids=participating_node_ids,
                    )
                    if cleanup_receipt is None:
                        raise RuntimeError("route_cleanup_receipt_unsealed")
                if isinstance(exc, InferenceCancelled):
                    raise exc
                self._record_incident(
                    state="request_failed_closed",
                    reason=reason,
                    request_id=request_id,
                )
                raise

            with self._lock:
                active = self._active_route_requests.get(request_id)
                if active is not None:
                    active["terminal"] = True
            self._request_outputs[request_id] = output
            self._request_inputs[request_id] = tuple(token_ids)
            self._request_limits[request_id] = max_new_tokens
            self._request_entry_nodes[request_id] = entry_node_id
            completed_at = time.monotonic()
            after_peers = self._peer_counters()
            assert before_peers is not None
            first_token_at = token_times[0] if token_times else None
            tpot_ms = None
            if len(token_times) > 1:
                tpot_ms = (
                    (token_times[-1] - token_times[0])
                    * 1_000.0
                    / (len(token_times) - 1)
                )
            self._recent_inferences.append(
                {
                    "context_tokens": len(token_ids),
                    "output_tokens": len(output),
                    "prefill_ms": (
                        None
                        if prefill_completed_at is None
                        else (prefill_completed_at - started_at) * 1_000.0
                    ),
                    "ttft_ms": (
                        None
                        if first_token_at is None
                        else (first_token_at - started_at) * 1_000.0
                    ),
                    "tpot_ms": tpot_ms,
                    "total_ms": (completed_at - started_at) * 1_000.0,
                    "peer_counter_deltas": [
                        {
                            "node_id": node_id,
                            "frames_sent": (
                                after_peers[node_id]["frames_sent"]
                                - before_peers[node_id]["frames_sent"]
                            ),
                            "frames_received": (
                                after_peers[node_id]["frames_received"]
                                - before_peers[node_id]["frames_received"]
                            ),
                            "applied_operation_count": (
                                after_peers[node_id]["applied_operation_count"]
                                - before_peers[node_id]["applied_operation_count"]
                            ),
                        }
                        for node_id in sorted(after_peers)
                    ],
                }
            )
            return InferenceResult(request_id=request_id, token_ids=output)

    def qualify_replica_concurrency(
        self,
        requests: Sequence[Sequence[int]],
        *,
        max_new_tokens: int,
        request_id_prefix: str,
    ) -> Mapping[str, Any]:
        """Run two overlapping requests and expose their immutable Router tracks.

        This is a qualification seam, not the browser request gateway. It keeps
        both requests admitted at once, advances decode round-robin, and returns
        privacy-reduced path and timing evidence for the M18 physical gate.
        """

        with self._lock:
            if not self.is_alive():
                raise RuntimeError("route_not_open")
            if (
                len(requests) != 2
                or not isinstance(max_new_tokens, int)
                or isinstance(max_new_tokens, bool)
                or max_new_tokens < 2
                or not isinstance(request_id_prefix, str)
                or not request_id_prefix
            ):
                raise ValueError("invalid_replica_qualification_request")
            normalized = tuple(tuple(tokens) for tokens in requests)
            if any(
                not tokens
                or any(
                    not isinstance(token, int) or isinstance(token, bool) or token < 0
                    for token in tokens
                )
                for tokens in normalized
            ):
                raise ValueError("invalid_replica_qualification_request")

            if (
                len(self._graph.stages) == 1
                and len(self._graph.stages[0].placements) >= 2
            ):
                entry_node_ids = tuple(
                    placement.node_id
                    for placement in self._graph.stages[0].placements[:2]
                )
            else:
                entry_node_ids = (self._plan["entry_node_id"],) * 2
            before_peers = self._peer_counters()
            started_at = time.monotonic()
            admission_barrier = threading.Barrier(2)

            def execute_track(
                index: int, token_ids: tuple[int, ...], entry_node_id: str
            ) -> dict[str, Any]:
                request_id = f"{request_id_prefix}-{index}"
                request = {
                    **self._plan["request"],
                    "request_id": request_id,
                    "prompt_token_ids": list(token_ids),
                    "max_new_tokens": max_new_tokens,
                    "expected_new_tokens": max_new_tokens,
                }
                payload: dict[str, Any] = {"request": request}
                if len(self._graph.stages) == 1:
                    local_placements = tuple(
                        placement
                        for placement in self._graph.stages[0].placements
                        if placement.node_id == entry_node_id
                    )
                    if len(local_placements) != 1:
                        raise RuntimeError("replica_entry_placement_invalid")
                    selected_placement_id = local_placements[0].placement_id
                    payload["excluded_placement_ids"] = [
                        placement.placement_id
                        for placement in self._graph.stages[0].placements
                        if placement.placement_id != selected_placement_id
                    ]
                command_started_offset_ms = (time.monotonic() - started_at) * 1_000.0
                try:
                    try:
                        response = self._sessions[entry_node_id].send(
                            command_id=self._command_id(
                                entry_node_id, "replica-infer-start"
                            ),
                            command="infer_start",
                            payload=payload,
                        )
                        observation = self._verify_observation(
                            entry_node_id,
                            response,
                            event="inference_started",
                        )
                    except BaseException as exc:
                        remote_code = getattr(exc, "remote_code", None)
                        code = remote_code if isinstance(remote_code, str) else str(exc)
                        raise RuntimeError(
                            f"replica_admission_rejected:{entry_node_id}:{code}"
                        ) from exc
                    details = observation.get("details")
                    path = details.get("path") if isinstance(details, Mapping) else None
                    output = self._output_tokens(observation)
                    if (
                        not isinstance(path, Mapping)
                        or not isinstance(path.get("path_id"), str)
                        or not isinstance(path.get("path_attempt"), int)
                        or not isinstance(path.get("placement_ids"), list)
                        or not path["placement_ids"]
                        or any(
                            not isinstance(item, str) or not item
                            for item in path["placement_ids"]
                        )
                    ):
                        raise RuntimeError("replica_qualification_path_missing")
                    record = {
                        "request_id": request_id,
                        "initial_status": details.get("status"),
                        "status": details.get("status"),
                        "output": output,
                        "path_id": path["path_id"],
                        "path_attempt": path["path_attempt"],
                        "placement_ids": tuple(path["placement_ids"]),
                        "entry_node_id": entry_node_id,
                        "admitted_offset_ms": (time.monotonic() - started_at) * 1_000.0,
                    }
                    admission_barrier.wait()
                    record["decode_started_offset_ms"] = (
                        time.monotonic() - started_at
                    ) * 1_000.0
                    while (
                        record["status"] == "DECODING"
                        and len(record["output"]) < max_new_tokens
                    ):
                        response = self._sessions[record["entry_node_id"]].send(
                            command_id=self._command_id(
                                record["entry_node_id"], "replica-infer-decode"
                            ),
                            command="infer_decode",
                            payload={"request_id": record["request_id"], "count": 1},
                        )
                        observation = self._verify_observation(
                            record["entry_node_id"],
                            response,
                            event="inference_decoded",
                        )
                        output = self._output_tokens(observation)
                        if len(output) <= len(record["output"]):
                            if observation["details"].get("status") == "COMPLETED":
                                record["status"] = "COMPLETED"
                                break
                            raise RuntimeError("replica_qualification_decode_stalled")
                        record["output"] = output
                        record["status"] = observation["details"].get("status")
                    record["completed_offset_ms"] = (
                        time.monotonic() - started_at
                    ) * 1_000.0
                    record["active_compute_elapsed_ms"] = (
                        record["admitted_offset_ms"]
                        - command_started_offset_ms
                        + record["completed_offset_ms"]
                        - record["decode_started_offset_ms"]
                    )
                    return record
                except BaseException:
                    admission_barrier.abort()
                    raise

            with ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="m18-replica"
            ) as executor:
                futures = [
                    executor.submit(execute_track, index, token_ids, entry_node_id)
                    for index, (token_ids, entry_node_id) in enumerate(
                        zip(normalized, entry_node_ids, strict=True)
                    )
                ]
                records = [future.result() for future in futures]

            overlapping_at_admission = all(
                record["initial_status"] == "DECODING" for record in records
            ) and max(record["admitted_offset_ms"] for record in records) < min(
                record["completed_offset_ms"] for record in records
            )

            self._snapshot_all()
            after_peers = self._peer_counters()
            completed_at = time.monotonic()
            tracks = [record["placement_ids"] for record in records]
            for record, tokens in zip(records, normalized, strict=True):
                request_id = record["request_id"]
                self._request_inputs[request_id] = tokens
                self._request_outputs[request_id] = tuple(record["output"])
                self._request_limits[request_id] = max_new_tokens
                self._request_entry_nodes[request_id] = record["entry_node_id"]
            return {
                "protocol": "mycelium.physical_replica_concurrency.v1",
                "deployment_id": self._graph.deployment_id,
                "request_count": 2,
                "overlapping_at_admission": overlapping_at_admission,
                "distinct_tracks": tracks[0] != tracks[1],
                "elapsed_ms": (completed_at - started_at) * 1_000.0,
                "requests": [
                    {
                        "request_id": record["request_id"],
                        "path_id": record["path_id"],
                        "path_attempt": record["path_attempt"],
                        "entry_node_id": record["entry_node_id"],
                        "placement_ids": list(record["placement_ids"]),
                        "prompt_token_count": len(tokens),
                        "output_token_count": len(record["output"]),
                        "output_digest": "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                list(record["output"]), separators=(",", ":")
                            ).encode("utf-8")
                        ).hexdigest(),
                        "admitted_offset_ms": record["admitted_offset_ms"],
                        "active_compute_elapsed_ms": record[
                            "active_compute_elapsed_ms"
                        ],
                    }
                    for record, tokens in zip(records, normalized, strict=True)
                ],
                "peer_counter_deltas": [
                    {
                        "node_id": node_id,
                        "frames_sent": after_peers[node_id]["frames_sent"]
                        - before_peers[node_id]["frames_sent"],
                        "frames_received": after_peers[node_id]["frames_received"]
                        - before_peers[node_id]["frames_received"],
                        "applied_operation_count": after_peers[node_id][
                            "applied_operation_count"
                        ]
                        - before_peers[node_id]["applied_operation_count"],
                    }
                    for node_id in sorted(after_peers)
                ],
                "route_ready": False,
            }

    def measure_replica_saturation(
        self,
        token_ids: Sequence[int],
        *,
        expected_output_token_ids: Sequence[int],
        request_counts_by_node: Mapping[str, int],
        request_id_prefix: str,
    ) -> Mapping[str, Any]:
        """Measure weighted steady-state throughput across complete local replicas."""

        with self._lock:
            if not self.is_alive() or len(self._graph.stages) != 1:
                raise RuntimeError("replica_saturation_unavailable")
            placements = self._graph.stages[0].placements
            placement_by_node = {
                placement.node_id: placement for placement in placements
            }
            if (
                len(placement_by_node) < 2
                or set(request_counts_by_node) != set(placement_by_node)
                or any(
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 1
                    or count > 64
                    for count in request_counts_by_node.values()
                )
                or not request_id_prefix
            ):
                raise ValueError("invalid_replica_saturation_request")
            prompt = tuple(token_ids)
            expected_output = tuple(expected_output_token_ids)
            if not prompt or not expected_output:
                raise ValueError("invalid_replica_saturation_request")

            started_at = time.monotonic()
            start_barrier = threading.Barrier(len(placement_by_node))

            def execute_node(node_id: str) -> list[dict[str, Any]]:
                placement = placement_by_node[node_id]
                excluded = [
                    candidate.placement_id
                    for candidate in placements
                    if candidate.placement_id != placement.placement_id
                ]
                records: list[dict[str, Any]] = []
                try:
                    start_barrier.wait()
                    for ordinal in range(request_counts_by_node[node_id]):
                        request_id = f"{request_id_prefix}-{node_id}-{ordinal}"
                        request = {
                            **self._plan["request"],
                            "request_id": request_id,
                            "prompt_token_ids": list(prompt),
                            "max_new_tokens": len(expected_output),
                            "expected_new_tokens": len(expected_output),
                        }
                        request_started = time.monotonic()
                        response = self._sessions[node_id].send(
                            command_id=self._command_id(
                                node_id, "replica-saturation-start"
                            ),
                            command="infer_start",
                            payload={
                                "request": request,
                                "excluded_placement_ids": excluded,
                            },
                        )
                        observation = self._verify_observation(
                            node_id, response, event="inference_started"
                        )
                        details = observation.get("details")
                        path = (
                            details.get("path")
                            if isinstance(details, Mapping)
                            else None
                        )
                        output = self._output_tokens(observation)
                        status = (
                            details.get("status")
                            if isinstance(details, Mapping)
                            else None
                        )
                        if not isinstance(path, Mapping) or path.get(
                            "placement_ids"
                        ) != [placement.placement_id]:
                            raise RuntimeError("replica_saturation_path_invalid")
                        while status == "DECODING" and len(output) < len(
                            expected_output
                        ):
                            response = self._sessions[node_id].send(
                                command_id=self._command_id(
                                    node_id, "replica-saturation-decode"
                                ),
                                command="infer_decode",
                                payload={"request_id": request_id, "count": 1},
                            )
                            observation = self._verify_observation(
                                node_id, response, event="inference_decoded"
                            )
                            next_output = self._output_tokens(observation)
                            if len(next_output) <= len(output):
                                raise RuntimeError("replica_saturation_decode_stalled")
                            output = next_output
                            status = observation["details"].get("status")
                        if output != expected_output:
                            raise RuntimeError("replica_saturation_token_mismatch")
                        records.append(
                            {
                                "request_id": request_id,
                                "node_id": node_id,
                                "path_id": path["path_id"],
                                "path_attempt": path["path_attempt"],
                                "placement_ids": [placement.placement_id],
                                "elapsed_ms": (time.monotonic() - request_started)
                                * 1_000.0,
                                "output_token_count": len(output),
                                "output_digest": "sha256:"
                                + hashlib.sha256(
                                    json.dumps(
                                        list(output), separators=(",", ":")
                                    ).encode("utf-8")
                                ).hexdigest(),
                            }
                        )
                    return records
                except BaseException:
                    start_barrier.abort()
                    raise

            with ThreadPoolExecutor(
                max_workers=len(placement_by_node),
                thread_name_prefix="m18-saturation",
            ) as executor:
                futures = {
                    node_id: executor.submit(execute_node, node_id)
                    for node_id in sorted(placement_by_node)
                }
                records_by_node = {
                    node_id: future.result() for node_id, future in futures.items()
                }
            self._snapshot_all()
            elapsed_ms = (time.monotonic() - started_at) * 1_000.0
            request_count = sum(request_counts_by_node.values())
            return {
                "protocol": "mycelium.physical_replica_saturation.v1",
                "deployment_id": self._graph.deployment_id,
                "request_count": request_count,
                "elapsed_ms": elapsed_ms,
                "throughput_rps": request_count * 1_000.0 / elapsed_ms,
                "request_counts_by_node": dict(sorted(request_counts_by_node.items())),
                "tracks": [
                    {
                        "node_id": node_id,
                        "placement_id": placement_by_node[node_id].placement_id,
                        "request_count": len(records),
                        "request_elapsed_ms": [
                            record["elapsed_ms"] for record in records
                        ],
                    }
                    for node_id, records in records_by_node.items()
                ],
                "requests": [
                    record
                    for node_id in sorted(records_by_node)
                    for record in records_by_node[node_id]
                ],
                "route_ready": False,
            }

    def a10_target_verify(
        self,
        node_id: str,
        *,
        operation: Mapping[str, Any],
        deadline_monotonic_s: float,
    ) -> dict[str, Any]:
        """Carry one private A10 target-verification operation to a live target node.

        The route only transports the operation; the node's own runtime executes
        and decides it (``a10_target_verify``).  An unknown or exited session, a
        missed deadline or any response that is not the node's own verification
        record fails closed; nothing is retried or synthesized here.
        """

        with self._lock:
            session = self._sessions.get(node_id)
        if session is None or session.returncode is not None:
            raise RuntimeError("a10_target_verify_node_unavailable")
        response = self._send_before(
            node_id,
            command_id=self._command_id(node_id, "a10_target_verify"),
            command="a10_target_verify",
            payload=dict(operation),
            deadline_monotonic_s=deadline_monotonic_s,
        )
        result = response.get("result") if isinstance(response, Mapping) else None
        if (
            not isinstance(result, Mapping)
            or result.get("protocol")
            != "mycelium.target_verification_operation_record.v1"
            or result.get("execution_path") != "product_dispatcher"
        ):
            raise RuntimeError("a10_target_verify_response_invalid")
        return dict(result)

    def a10_batch_policy(
        self,
        node_id: str,
        *,
        policy: str,
        deadline_monotonic_s: float,
    ) -> dict[str, Any]:
        """Carry one A10 batch-policy switch to a live node.

        The route only transports the switch; the node applies it under its own
        control lock and answers with its receipt (``a10_batch_policy``).  An
        unknown or exited session, a missed deadline or any response that is not
        the node's receipt for the same policy fails closed.
        """

        with self._lock:
            session = self._sessions.get(node_id)
        if session is None or session.returncode is not None:
            raise RuntimeError("a10_batch_policy_node_unavailable")
        response = self._send_before(
            node_id,
            command_id=self._command_id(node_id, "a10_batch_policy"),
            command="a10_batch_policy",
            payload={"policy": policy},
            deadline_monotonic_s=deadline_monotonic_s,
        )
        result = response.get("result") if isinstance(response, Mapping) else None
        if (
            not isinstance(result, Mapping)
            or result.get("protocol") != "mycelium.a10_batch_policy_receipt.v1"
            or result.get("policy") != policy
        ):
            raise RuntimeError("a10_batch_policy_response_invalid")
        return dict(result)

    def release_request(self, request_id: str) -> None:
        """Forget prompt/output token material after the gateway releases a request."""

        with self._lock:
            self._request_inputs.pop(request_id, None)
            self._request_outputs.pop(request_id, None)
            self._request_limits.pop(request_id, None)
            self._request_entry_nodes.pop(request_id, None)
            self._request_locks.pop(request_id, None)
            self._active_route_requests.pop(request_id, None)
            getattr(self, "_pending_publisher_generations", {}).pop(
                request_id,
                None,
            )
            self._request_cleanup_receipts.pop(request_id, None)
            getattr(self, "_cleanup_snapshots_by_request", {}).pop(request_id, None)

    def request_cleanup_receipt_scoped(
        self,
        request_id: str,
        *,
        request_attempt: int,
        path_id: str,
        path_attempt: int,
        path_digest: str,
        cleanup_owner_id: str,
    ) -> Mapping[str, Any] | None:
        """Read a cleanup receipt only for the owner's exact immutable subject."""

        expected = {
            "request_id": request_id,
            "request_attempt": request_attempt,
            "path_id": path_id,
            "path_attempt": path_attempt,
            "path_digest": path_digest,
            "cleanup_owner_id": cleanup_owner_id,
        }
        with self._lock:
            receipt = self._request_cleanup_receipts.get(request_id)
            if receipt is None or any(
                receipt.get(field) != value for field, value in expected.items()
            ):
                return None
            return dict(receipt)

    def release_request_scoped(
        self,
        request_id: str,
        *,
        request_attempt: int,
        path_id: str,
        path_attempt: int,
        path_digest: str,
        cleanup_owner_id: str,
    ) -> None:
        """Release only after exact cleanup proof; stale attempts cannot erase state."""

        receipt = self.request_cleanup_receipt_scoped(
            request_id,
            request_attempt=request_attempt,
            path_id=path_id,
            path_attempt=path_attempt,
            path_digest=path_digest,
            cleanup_owner_id=cleanup_owner_id,
        )
        if receipt is None:
            raise RuntimeError("request_scoped_release_cleanup_unproven")
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None or any(
                active.get(field) != value
                for field, value in (
                    ("request_attempt", request_attempt),
                    ("path_id", path_id),
                    ("path_attempt", path_attempt),
                    ("path_digest", path_digest),
                )
            ):
                raise RuntimeError("request_scoped_release_identity_mismatch")
            self._request_inputs.pop(request_id, None)
            self._request_outputs.pop(request_id, None)
            self._request_limits.pop(request_id, None)
            self._request_entry_nodes.pop(request_id, None)
            self._request_locks.pop(request_id, None)
            self._active_route_requests.pop(request_id, None)
            getattr(self, "_pending_publisher_generations", {}).pop(
                request_id,
                None,
            )
            self._request_cleanup_receipts.pop(request_id, None)
            getattr(self, "_cleanup_snapshots_by_request", {}).pop(request_id, None)
            reader_incidents = getattr(self, "_response_reader_failure_incidents", {})
            for key in tuple(reader_incidents):
                if key[0] == request_id:
                    reader_incidents.pop(key, None)

    def request_cleanup_receipt(self, request_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            receipt = self._request_cleanup_receipts.get(request_id)
            return None if receipt is None else dict(receipt)

    def _peer_counters(self) -> dict[str, dict[str, int]]:
        counters: dict[str, dict[str, int]] = {}
        for node_id in sorted(self._peers):
            high_water = {
                "frames_sent": 0,
                "frames_received": 0,
                "applied_operation_count": 0,
            }
            observations = (
                self._last_snapshots.get(node_id, {}),
                getattr(self, "_last_health", {}).get(node_id, {}),
            )
            for observation in observations:
                details = observation.get("details", {})
                if not isinstance(details, Mapping):
                    continue
                transport = details.get("transport", {})
                if not isinstance(transport, Mapping):
                    transport = {}
                transport_counters = details.get("transport_counters", {})
                if not isinstance(transport_counters, Mapping):
                    transport_counters = {}
                runtime = details.get("runtime", {})
                if not isinstance(runtime, Mapping):
                    runtime = {}
                runtime_counters = details.get("runtime_counters", {})
                if not isinstance(runtime_counters, Mapping):
                    runtime_counters = {}
                observed = {
                    "frames_sent": int(
                        transport.get(
                            "remote_frames_sent",
                            transport_counters.get("remote_frames_sent", 0),
                        )
                    ),
                    "frames_received": int(
                        transport.get(
                            "remote_frames_received",
                            transport_counters.get("remote_frames_received", 0),
                        )
                    ),
                    "applied_operation_count": int(
                        runtime.get(
                            "applied_operation_count",
                            runtime_counters.get("applied_operation_count", 0),
                        )
                    ),
                }
                high_water = {
                    key: max(high_water[key], observed[key]) for key in high_water
                }
            counters[node_id] = high_water
        return counters

    def counters(self) -> RouteCounters:
        peer_counters = self._peer_counters()
        return RouteCounters(
            frames_sent=sum(item["frames_sent"] for item in peer_counters.values()),
            frames_received=sum(
                item["frames_received"] for item in peer_counters.values()
            ),
            applied_operation_count=sum(
                item["applied_operation_count"] for item in peer_counters.values()
            ),
            fatal=self._fatal,
        )

    def peer_liveness_overview(self) -> dict[str, dict[str, Any]]:
        """Per-peer liveness for diagnostics: label (fresh/stale/quarantined/failed), last seen, misses."""

        labels = {
            LivenessState.FRESH: "fresh",
            LivenessState.RECOVERED: "fresh",
            LivenessState.SUSPECT: "stale",
            LivenessState.QUARANTINED: "quarantined",
        }
        now_ms = self._monotonic_ms()
        overview: dict[str, dict[str, Any]] = {}
        for item in self._liveness.snapshots():
            if item.identity.kind is not SubjectKind.PEER:
                continue
            overview[item.identity.subject_id] = {
                "state": item.state.value,
                "label": labels.get(item.state, "failed"),
                "last_seen_seconds_ago": round(max(0, now_ms - item.last_fresh_ms) / 1000.0, 3),
                "consecutive_misses": item.consecutive_misses,
                "recovery_observations": item.consecutive_signed_fresh,
                "next_probe_in_seconds": round(max(0, item.next_keepalive_due_ms - now_ms) / 1000.0, 3),
            }
        return overview

    def a4_liveness_status(self) -> Mapping[str, Any]:
        """Return the detector-owned privacy-reduced liveness projection."""

        from mycelium_live.a4_contracts import validate_traffic_liveness

        snapshots = self._liveness.snapshots()
        incidents = self._liveness.incidents()
        return validate_traffic_liveness(
            {
                "protocol": "mycelium.traffic_liveness.v1",
                "deployment_id": self._graph.deployment_id,
                "generated_at_monotonic_ms": int(time.monotonic() * 1_000),
                "subjects": [
                    {
                        "subject_id": item.identity.subject_id,
                        "kind": item.identity.kind.value,
                        "membership_generation": item.identity.membership_generation,
                        "state": item.state.value,
                        "last_fresh_ms": item.last_fresh_ms,
                        "last_observed_ms": item.last_observed_ms,
                        "next_keepalive_due_ms": item.next_keepalive_due_ms,
                        "consecutive_misses": item.consecutive_misses,
                        "last_source": item.last_source.value,
                    }
                    for item in snapshots
                ],
                "incidents": [
                    {
                        "sequence": item.sequence,
                        "source": item.source.value,
                        "scope": item.scope,
                        "subject_id": item.subject.subject_id,
                        "membership_generation": item.subject.membership_generation,
                        "observed_at_ms": item.observed_at_ms,
                        "affected_track_ids": list(item.affected_track_ids),
                        "action": item.action,
                        "outcome": item.outcome,
                        "detection_latency_ms": item.detection_latency_ms,
                        "within_detection_budget": item.within_detection_budget,
                    }
                    for item in incidents
                ],
                "deployment_fatal_reason": self._liveness.deployment_fatal_reason,
            }
        )

    _peer_exit_successor_authority: Callable[..., bool] | None = None
    _peer_exit_claim_lock: Any = None

    def set_peer_exit_successor_authority(
        self,
        authority: Callable[..., bool] | None,
    ) -> None:
        """Install the A7 owner that may claim one exited non-entry participant."""

        with self._lock:
            if self._closed:
                raise RuntimeError("route_closed")
            if self._peer_exit_claim_lock is None:
                self._peer_exit_claim_lock = threading.Lock()
            self._peer_exit_successor_authority = authority

    def _bound_participants(
        self,
        request_id: str,
        fallback: frozenset[str],
    ) -> frozenset[str]:
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if active is None:
                return fallback
            return frozenset(active["participating_node_ids"])

    def _exited_participant_ids(self, node_ids: frozenset[str]) -> frozenset[str]:
        """The given participants whose node process has exited (returncode set).

        Only an observed exit status counts: a peer that is merely disconnected,
        stalled (SIGSTOP) or slow still has a live process and must prove its
        own cleanup.
        """

        with self._lock:
            sessions = dict(getattr(self, "_sessions", None) or {})
        return frozenset(
            node_id
            for node_id in node_ids
            if type(getattr(sessions.get(node_id), "returncode", None)) is int
        )

    def _bind_covered_peer_exit(
        self,
        authority: Callable[..., bool],
        request_id: str,
        node_id: str,
    ) -> bool:
        """Let the request's entry own one exited participant (A7), or refuse.

        The authority sends the exact control identity and the exited node to
        the request's live entry and returns True only when that entry accepted
        request-scoped ownership (fenced KV-successor cutover, or its own
        truthful fallback reported through the in-flight decode). The exited
        peer then leaves this request's cleanup participants: process exit is
        its cleanup proof, as for A6. Any error or refusal keeps the existing
        interruption below.
        """

        with self._lock:
            active = self._active_route_requests.get(request_id)
            if (
                active is None
                or active.get("terminal") is True
                or active.get("cancellation_requested") is True
                or active.get("control_bound") is not True
                or node_id == active["entry_node_id"]
                or node_id not in active["participating_node_ids"]
            ):
                return False
            entry_node_id = active["entry_node_id"]
            control = self._request_control_identity(request_id)

        def send_to_entry(
            command: str,
            payload: Mapping[str, Any],
            deadline_monotonic_s: float,
        ) -> dict[str, Any]:
            response = self._send_before(
                entry_node_id,
                command_id=self._command_id(entry_node_id, command),
                command=command,
                payload=dict(payload),
                deadline_monotonic_s=deadline_monotonic_s,
            )
            return self._verify_observation(entry_node_id, response, event=command)

        try:
            accepted = authority(
                request_id=request_id,
                lost_node_id=node_id,
                control=dict(control),
                send_to_entry=send_to_entry,
            )
        except BaseException:
            return False
        if accepted is not True:
            return False
        with self._lock:
            active = self._active_route_requests.get(request_id)
            if (
                active is None
                or active.get("terminal") is True
                or active.get("cancellation_requested") is True
                or node_id not in active["participating_node_ids"]
                or any(active.get(field) != value for field, value in control.items())
            ):
                return False
            active["participating_node_ids"] = frozenset(
                active["participating_node_ids"]
            ) - {node_id}
        self._record_scoped_runtime_incident(
            request_id=request_id,
            subject_id=node_id,
            scope="request",
            reason="node_process_exited",
        )
        return True

    def _handle_lost_peer_processes(self) -> None:
        """Interrupt only active requests whose physical participant exited."""

        observed_at_s = time.monotonic()
        with self._lock:
            if not self._open or self._closed:
                return
            lost = tuple(
                node_id
                for node_id, session in sorted(self._sessions.items())
                if session.returncode is not None
            )
        for node_id in lost:
            authority = self._peer_exit_successor_authority
            # A7: serialise snapshot and claim so a concurrent detection caller
            # cannot act on a track another caller is handing to its successor.
            # Never taken under self._lock; absent when no authority exists.
            claim_lock = self._peer_exit_claim_lock if authority is not None else None
            if claim_lock is not None:
                claim_lock.acquire()
            try:
                with self._lock:
                    active_tracks = tuple(
                        request_id
                        for request_id, active in self._active_route_requests.items()
                        if active.get("terminal") is not True
                        and active.get("cancellation_requested") is not True
                        and node_id in active["participating_node_ids"]
                    )
                covered = frozenset(
                    request_id
                    for request_id in active_tracks
                    if authority is not None
                    and self._bind_covered_peer_exit(authority, request_id, node_id)
                )
            finally:
                if claim_lock is not None:
                    claim_lock.release()
            subject = self._liveness_subjects.get(node_id)
            if subject is not None:
                snapshot = self._liveness.subject_snapshot(subject)
                if snapshot is not None and snapshot.state.value not in {
                    "failed",
                    "quarantined",
                }:
                    now_ms = int(observed_at_s * 1_000)
                    if active_tracks:
                        self._liveness.record_active_failure(
                            subject,
                            failure_started_at_ms=now_ms,
                            observed_at_ms=now_ms,
                            scope="peer",
                            affected_track_ids=active_tracks,
                            verified=True,
                        )
                    else:
                        self._liveness.record_nonparticipating_peer_exit(
                            subject,
                            observed_at_ms=now_ms,
                        )
            interruption_deadline = observed_at_s + _MAX_CANCELLATION_AND_CLEANUP_SECONDS
            for request_id in active_tracks:
                if request_id in covered:
                    continue
                with self._lock:
                    # The interruption below is this route observing a
                    # participant's process exit, not an owner cancellation.
                    # The request's worker may see its in-flight command
                    # interrupted (``request_cancelled``) before that peer's
                    # response reader delivers EOF; it records the exit as
                    # the request's runtime failure from this mark.
                    active = self._active_route_requests.get(request_id)
                    if (
                        active is not None
                        and active.get("terminal") is not True
                        and active.get("cancellation_requested") is not True
                    ):
                        active["lost_peer_node_ids"] = frozenset(
                            active.get("lost_peer_node_ids", ())
                        ) | {node_id}
                try:
                    self.cancel_request(
                        request_id,
                        deadline_monotonic_s=interruption_deadline,
                    )
                except BaseException:
                    # The request worker remains responsible for fail-closed
                    # terminal publication and exact cleanup proof.  A dead
                    # fanout leaf must not prevent interruption of live peers.
                    pass
            self._record_incident(
                state="peer_process_lost",
                reason="route_peer_process_lost",
                request_id=None,
            )

    def public_status(self) -> Mapping[str, Any]:
        """Return a bounded, prompt-free projection of the running route."""

        self._handle_lost_peer_processes()
        self._sync_replica_loss_from_liveness()
        with self._lock:
            stages: list[dict[str, Any]] = []
            placements_by_node: dict[str, list[dict[str, Any]]] = {
                node_id: [] for node_id in self._peers
            }
            decode_modes: set[str] = set()
            for stage in self._graph.stages:
                for placement in stage.placements:
                    projected = {
                        "stage_id": stage.stage_id,
                        "placement_id": placement.placement_id,
                        "node_id": placement.node_id,
                        "runtime_backend": placement.runtime_backend,
                        "start_layer": stage.layer_range.start_layer,
                        "end_layer_exclusive": (stage.layer_range.end_layer_exclusive),
                        "component_roles": list(stage.component_roles),
                    }
                    stages.append(projected)
                    placements_by_node.setdefault(placement.node_id, []).append(
                        projected
                    )
            peers: list[dict[str, Any]] = []
            for node_id in sorted(self._peers):
                observation = self._last_snapshots.get(node_id, {})
                details = observation.get("details", {})
                health_observation = self._last_health.get(node_id, {})
                runtime = details.get("runtime", {})
                interruptibility = details.get("interruptibility")
                resources = details.get("host_resources", {})
                mode = runtime.get("mode")
                if isinstance(mode, str):
                    decode_modes.add(mode)
                release_counts = runtime.get("release_counts", {})
                architecture = runtime.get("architecture")
                architecture_modes = (
                    resources.get("decode_modes_by_architecture", {})
                    if isinstance(resources, Mapping)
                    else {}
                )
                supported_modes = (
                    architecture_modes.get(architecture, [])
                    if isinstance(architecture_modes, Mapping)
                    and isinstance(architecture, str)
                    else []
                )
                peers.append(
                    {
                        "node_id": node_id,
                        "placements": placements_by_node.get(node_id, []),
                        "placement_counters": {
                            placement["placement_id"]: {
                                field: counts[field]
                                for field in (
                                    "prefill_operation_count", "decode_operation_count",
                                    "active_state_count", "active_kv_bytes",
                                )
                            }
                            for placement in placements_by_node.get(node_id, [])
                            if isinstance(runtime.get("placement_counters"), Mapping)
                            and isinstance(
                                counts := runtime["placement_counters"].get(placement["placement_id"]),
                                Mapping,
                            )
                            and all(
                                type(counts.get(field)) is int and counts[field] >= 0
                                for field in (
                                    "prefill_operation_count", "decode_operation_count",
                                    "active_state_count", "active_kv_bytes",
                                )
                            )
                        },
                        **_peer_counter_projection(observation, health_observation),
                        "decode_mode": mode if isinstance(mode, str) else None,
                        "architecture": (
                            architecture if isinstance(architecture, str) else None
                        ),
                        "supported_decode_modes": (
                            [str(value) for value in supported_modes]
                            if isinstance(supported_modes, (list, tuple))
                            else []
                        ),
                        **_public_transport_health(health_observation),
                        "active_kv_state_count": int(
                            runtime.get("active_state_count", 0)
                        ),
                        "active_kv_bytes": int(runtime.get("active_kv_bytes", 0)),
                        "peak_kv_bytes": int(runtime.get("peak_kv_bytes", 0)),
                        "prefill_input_token_count": int(
                            runtime.get("prefill_input_token_count", 0)
                        ),
                        "decode_input_token_count": int(
                            runtime.get("decode_input_token_count", 0)
                        ),
                        "activation_output_bytes": int(
                            runtime.get("activation_output_bytes", 0)
                        ),
                        "current_position": (
                            int(runtime["current_position"])
                            if type(runtime.get("current_position")) is int
                            else None
                        ),
                        "release_state": str(runtime.get("release_state", "unknown")),
                        "last_release_reason": (
                            str(runtime["last_release_reason"])
                            if isinstance(runtime.get("last_release_reason"), str)
                            else None
                        ),
                        "retained_result_count": int(
                            runtime.get("retained_result_count", 0)
                        ),
                        "release_counts": (
                            {
                                str(reason): int(count)
                                for reason, count in sorted(release_counts.items())
                            }
                            if isinstance(release_counts, Mapping)
                            else {}
                        ),
                        "interruptibility": _public_interruptibility(interruptibility),
                        # A10: the node-co-signed runtime publication (stage-compute
                        # intervals and runtime batch observations), re-validated
                        # closed and privacy-reduced; absent or invalid -> None.
                        "a10_publication": _public_a10_publication(
                            details.get("a10_publication")
                        ),
                    }
                )
            identity_material = json.dumps(
                {
                    "deployment_id": self._plan["deployment_id"],
                    "topology_version": self._graph.topology_version,
                    "endpoints": [
                        self._endpoints[node_id]["endpoint_id"]
                        for node_id in sorted(self._endpoints)
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            aggregate = self.counters()
            status = {
                "protocol": "mycelium.live_route_status.v1",
                "route_alive": self.is_alive(),
                "simulated": False,
                "route_identity_digest": (
                    "sha256:" + hashlib.sha256(identity_material).hexdigest()
                ),
                "deployment_id": self._plan["deployment_id"],
                "model_id": self._graph.model_id,
                "topology_version": self._graph.topology_version,
                "decode_mode": (
                    next(iter(decode_modes)) if len(decode_modes) == 1 else "mixed"
                ),
                "counters": {
                    "frames_sent": aggregate.frames_sent,
                    "frames_received": aggregate.frames_received,
                    "applied_operation_count": (aggregate.applied_operation_count),
                    "fatal": aggregate.fatal,
                },
                "stages": stages,
                "peers": peers,
                "recent_inferences": list(self._recent_inferences),
                "incidents": list(self._incidents),
                "liveness": self.a4_liveness_status(),
                "concurrency_liveness_qualification": (
                    dict(self._a4_qualification)
                    if self._a4_qualification is not None
                    else self._unqualified_a4_status()
                ),
                "replica_track_qualification": [
                    dict(document) for document in self._replica_track_qualifications
                ],
                "replica_loss_placement_ids": sorted(self._replica_loss_placement_ids),
            }
            if self._placement_projection is not None:
                status["placement"] = json.loads(json.dumps(self._placement_projection))
            if self._topology_projection is not None:
                status["topology"] = json.loads(json.dumps(self._topology_projection))
            return status

    def _unqualified_a4_status(self) -> dict[str, Any]:
        return {
            "protocol": "mycelium.product_concurrency_liveness_qualification.v1",
            "deployment_id": self._graph.deployment_id,
            "qualification_digest": "sha256:"
            + hashlib.sha256(b"a4-physical-proof-unavailable").hexdigest(),
            "maximum_concurrent_requests": 4,
            "cancellation_and_cleanup_bound_ms": mycelium_deadlines.COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS,
            "cooperative_interruption_proven": False,
            "request_scoped_cleanup_proven": False,
            "shared_process_termination_used": False,
            "publisher_generation_fencing_proven": False,
            "scoped_liveness_proven": False,
            "eligible": False,
            "evidence_digest": "sha256:"
            + hashlib.sha256(b"a4-physical-evidence-unavailable").hexdigest(),
        }

    def set_a4_qualification(self, document: Mapping[str, Any]) -> None:
        """Install only an externally sealed physical A4 qualification record."""

        from mycelium_live.a4_contracts import validate_product_qualification
        from mycelium_request_gateway.contracts import qualification_digest

        validated = validate_product_qualification(document)
        current = self._deployment_qualification
        if (
            validated["deployment_id"] != self._graph.deployment_id
            or validated["eligible"] is not True
            or current is None
            or validated["qualification_digest"] != qualification_digest(current)
        ):
            raise ValueError("a4_qualification_binding_mismatch")
        with self._lock:
            self._a4_qualification = dict(validated)

    def set_replica_track_qualification(
        self,
        documents: Sequence[Mapping[str, Any]],
    ) -> None:
        """Install validated replica-track qualifications (A5 qualifier output).

        Consume-only: the dispatcher reads these to select one of N qualified
        tracks per request. Each document must be a closed
        ``mycelium.replica_qualification.v1`` record; anything else fails
        closed and installs nothing.
        """

        from mycelium_replica_contracts import validate_replica_qualification

        if not isinstance(documents, Sequence) or isinstance(documents, (str, bytes)):
            raise ValueError("invalid_replica_qualification_documents")
        normalized = tuple(
            validate_replica_qualification(document) for document in documents
        )
        with self._lock:
            self._replica_track_qualifications = normalized

    def replica_track_qualifications(self) -> tuple[dict[str, Any], ...]:
        """Return the currently installed replica qualifications (validated)."""

        with self._lock:
            return self._replica_track_qualifications

    def mark_replica_loss_placement(self, placement_id: str) -> None:
        """Mark one placement lost; its tracks stop being selectable.

        A5-owned replica-loss state, bounded: one placement id per call. The
        dispatcher consumes this via ``replica_loss_placement_ids``.
        """

        if not isinstance(placement_id, str) or not placement_id:
            raise ValueError("invalid_replica_loss_placement")
        with self._lock:
            self._replica_loss_placement_ids = (
                self._replica_loss_placement_ids | frozenset({placement_id})
            )

    def replica_loss_placement_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._replica_loss_placement_ids)

    def _sync_replica_loss_from_liveness(self) -> None:
        """Mark placements on quarantined/failed liveness subjects as lost.

        A5 consumes A4's liveness authority without extending it: the A4
        detector decides when a subject is quarantined or failed; this
        method only reads the current snapshots and drops the affected
        replica tracks from the selectable set. Loss is monotonic for the
        lifetime of the serve (A5 never un-marks a placement).
        """

        from mycelium_qualification.replica_track import (
            placements_lost_by_liveness,
        )

        snapshots: dict[str, str] = {}
        for node_id, subject in self._liveness_subjects.items():
            snapshot = self._liveness.subject_snapshot(subject)
            if snapshot is not None:
                snapshots[node_id] = snapshot.state.value
        if not snapshots:
            return
        placement_nodes = (
            (placement.placement_id, placement.node_id)
            for stage in self._graph.stages
            for placement in stage.placements
        )
        lost = placements_lost_by_liveness(placement_nodes, snapshots)
        if not lost:
            return
        with self._lock:
            self._replica_loss_placement_ids = self._replica_loss_placement_ids | lost

    def membership_status(self, *, qualification: Any | None) -> Mapping[str, Any]:
        """Project durable members independently from execution-graph placement."""

        authority = self._seed_authority
        if authority is None:
            raise LiveSeedStateError("live_seed_authority_unavailable")
        now = time.time()
        members = authority.current_members()
        placed = {
            placement.node_id
            for stage in self._graph.stages
            for placement in stage.placements
        }
        native_nodes: list[dict[str, Any]] = []
        browser_workers: list[dict[str, Any]] = []
        route_alive = self.is_alive()
        route_qualified = (
            qualification is not None
            and getattr(qualification, "route_ready", False) is True
            and route_alive
        )
        for member in members:
            live_lease = float(member["lease_expires_at"]) > now
            peer_class = member["peer_class"]
            if peer_class in {"browser_http", "pixel_http"}:
                browser_workers.append(
                    {
                        "peer_id": member["node_id"],
                        "capability": "synthetic_browser_probe",
                        "state": "ready" if live_lease else "stale",
                        "expires_at_unix_ms": int(
                            float(member["lease_expires_at"]) * 1_000
                        ),
                    }
                )
                continue
            if not live_lease:
                membership_state = "revoked"
                connectivity = "stale"
            elif member["node_id"] not in placed:
                membership_state = "reachable"
                connectivity = "unknown"
            elif route_qualified:
                membership_state = "qualified"
                connectivity = "unknown"
            elif route_alive:
                membership_state = "assigned"
                connectivity = "unknown"
            else:
                membership_state = "reachable"
                connectivity = "unknown"
            native_nodes.append(
                {
                    "member_id": member["node_id"],
                    "capability": "native_inference_node",
                    "membership_state": membership_state,
                    "connectivity": connectivity,
                    "endpoint_id": None,
                }
            )
        return {
            "protocol": "mycelium.product_ui.swarm.v1",
            "native_nodes": native_nodes,
            "browser_workers": browser_workers,
        }

    def mint_native_invite(
        self,
        *,
        seed_url: str,
        ttl_seconds: int,
        nonce: str,
    ) -> Mapping[str, Any]:
        """Mint a target-device bundle from the live durable seed authority."""

        authority = self._seed_authority
        if authority is None:
            raise LiveSeedStateError("live_seed_authority_unavailable")
        authority.state_root.revalidate()
        return mint_invite_bundle(
            signer=authority.signer,
            swarm_id=authority.swarm_id,
            seed_url=seed_url,
            ttl_seconds=ttl_seconds,
            nonce=nonce,
            issued_at=time.time(),
        )

    def revoke_native_member(self, member_id: str) -> Mapping[str, Any]:
        """Fence a member and fail closed when it participates in the active route."""

        authority = self._seed_authority
        if authority is None:
            raise LiveSeedStateError("live_seed_authority_unavailable")
        placed = {
            placement.node_id
            for stage in self._graph.stages
            for placement in stage.placements
        }
        active_placement = member_id in placed and self.is_alive()
        member = next(
            (
                item
                for item in authority.current_members()
                if item["node_id"] == member_id
            ),
            None,
        )
        if member is None:
            raise LiveSeedStateError("member_unknown")
        try:
            result = revoke_seed_member(
                authority.state_root.path,
                node_id=member_id,
                expected_generation=int(member["generation"]),
                reason="product-ui-owner-revocation",
            )
        except SeedOperatorError as exc:
            raise LiveSeedStateError(exc.code) from exc
        if active_placement:
            self._fatal = "active_member_revoked"
            self._record_incident(
                state="fatal",
                reason="active_member_revoked",
                request_id=None,
            )
            self.close()
        return result

    def product_membership_records(self) -> tuple[dict[str, Any], ...]:
        authority = self._seed_authority
        if authority is None:
            raise LiveSeedStateError("live_seed_authority_unavailable")
        return authority.current_members()

    def product_assignment_records(self) -> tuple[dict[str, Any], ...]:
        """Return privacy-reduced assignment/load bindings from the validated plan."""

        snapshot = self._membership_snapshot
        if snapshot is None:
            raise LiveSeedStateError("live_assignment_authority_unavailable")
        offers = snapshot.get("assignment_offers")
        if not isinstance(offers, list):
            raise LiveSeedStateError("live_assignment_authority_unavailable")
        placement_by_assignment = {
            placement.assignment_id: (
                placement.placement_id,
                placement.load_proof_digest,
            )
            for stage in self._graph.stages
            for placement in stage.placements
        }
        records: list[dict[str, Any]] = []
        for envelope in offers:
            message = envelope.get("message") if isinstance(envelope, Mapping) else None
            if not isinstance(message, Mapping):
                raise LiveSeedStateError("live_assignment_authority_unavailable")
            assignment_id = message.get("assignment_id")
            placement = placement_by_assignment.get(assignment_id)
            if placement is None:
                raise LiveSeedStateError("live_assignment_binding_mismatch")
            stage_id, load_proof_digest = placement
            records.append(
                {
                    "assignment_id": assignment_id,
                    "node_id": message.get("recipient_node_id"),
                    "stage_id": stage_id,
                    "membership_generation": message.get("generation"),
                    "load_generation": message.get("load_generation"),
                    "assignment_digest": message.get("assignment_digest"),
                    "stage_pack_digest": message.get("stage_pack_digest"),
                    "load_proof_digest": load_proof_digest,
                }
            )
        return tuple(records)

    def product_pseudonym_salt(self) -> bytes:
        authority = self._seed_authority
        if authority is None:
            raise LiveSeedStateError("live_seed_authority_unavailable")
        return authority.product_pseudonym_salt()

    def product_internet_native_snapshot(self) -> dict[str, Any]:
        """Expose only verified, privacy-reduced A8 evidence to the product spine."""

        with self._lock:
            snapshots = json.loads(json.dumps(self._last_snapshots))
            route_available = (
                self._open
                and not self._closed
                and self._fatal is None
                and bool(self._sessions)
            )
        projection = _product_internet_native_projection(
            snapshots=snapshots,
            projection_key=self.product_pseudonym_salt(),
            now_unix_ms=int(time.time() * 1_000),
            route_available=route_available,
        )
        with self._lock:
            merged = _merge_product_activation_history(
                tuple(self._product_activation_history), projection
            )
            self._product_activation_history.clear()
            self._product_activation_history.extend(merged["activation_history"])
            return merged

    def _admit_observed_cleanup_bounds(
        self, signed_observations: Sequence[Mapping[str, Any]]
    ) -> None:
        """Refuse a peer that advertises a cleanup bound this route cannot honour.

        Reviewer decision 2 / PLAN D9: a peer may not advertise more cleanup time
        than the route can wait for.  ``admit_peer_cleanup_bound`` mints the named
        refusal (``peer_cleanup_bound_exceeds_route_window:<node>:advertised=<m>:
        admitted=<m>:window=<m>``) for exactly that comparison, and until now it
        was landed but wired **nowhere** - so a peer whose measured cleanup
        exceeded the window was admitted at startup and failed mid-request
        instead of being refused by name (`runs/wp5-cleanup-arithmetic/`: the
        relayed peer's 1.83 s / 3.18 s against the 1.5 s window).

        This is the qualification path's consumer of that advertisement:
        ``live_attestation`` is qualification-only, refreshes one post-cleanup
        snapshot per peer, and is the explicit consumer of ``interruptibility``
        (which carries the advertised ``maximum_total_cleanup_ms``).  The refusal
        therefore happens at start-up qualification - what the owner's standing
        rule requires before a physical round is spent - and never mid-request:
        one over-reaching peer fails the whole qualification closed, by name.

        An absent bound is not refused here: ``admit_peer_cleanup_bound`` returns
        ``None`` for it deliberately ("an absent assertion is not a passing one,
        but it is also not this predicate's business to invent one"), and the
        node's advertised bound is a closed, validated payload projection
        (``physical_inference_node.py``); inventing a bound here would refuse a
        peer for a fact this predicate cannot observe.
        """

        latest: dict[str, Mapping[str, Any]] = {}
        for envelope in signed_observations:
            observation = (
                envelope.get("observation") if isinstance(envelope, Mapping) else None
            )
            if not isinstance(observation, Mapping):
                continue
            if observation.get("event") != "snapshot":
                continue
            node_id = observation.get("node_id")
            if isinstance(node_id, str) and node_id:
                latest[node_id] = observation
        for node_id in sorted(latest):
            details = latest[node_id].get("details")
            interruptibility = (
                details.get("interruptibility") if isinstance(details, Mapping) else None
            )
            advertised_ms = (
                interruptibility.get("maximum_total_cleanup_ms")
                if isinstance(interruptibility, Mapping)
                else None
            )
            try:
                admit_peer_cleanup_bound(advertised_ms, node_id=node_id)
            except ValueError as error:
                # Re-raise unchanged: the predicate's message *is* the named
                # refusal, arithmetic included, and a wrapped code here would
                # hide the numbers a reader needs to see.
                raise RuntimeError(str(error)) from error

    def phone_qualification_bindings(self) -> dict[str, Any]:
        """Validated current seed and packaged load authority, outside the attestation.

        Recomputed on every call, so each qualification and renewal re-reads
        the pinned seed's member and applied-route rows together and re-checks
        the packaged proof. A refusal raises; nothing is returned for the
        qualifier to fall back on.
        """
        from .phone_load_authority import current_phone_load_bindings
        return current_phone_load_bindings(self)

    def live_attestation(self, *, request_id: str) -> dict[str, Any]:
        if request_id not in self._request_outputs or not self.is_alive():
            raise RuntimeError("live_attestation_unavailable")
        # Normal request cleanup records only the bounded signed receipt and
        # lightweight counters needed by the product path. Qualification is
        # the explicit consumer of full capacity, host, interruptibility, and
        # activation-plane evidence, so refresh that evidence here, after the
        # request's two-second cleanup deadline is no longer in flight.
        # Qualification counters must cover the just-finished request.  A
        # structurally complete cached snapshot can still predate that work;
        # freshness cannot be inferred from the presence of its transport
        # fields.  Always capture one post-cleanup full snapshot here.  This
        # path is qualification-only and runs after the request's strict
        # cleanup deadline, so it cannot contend with receipt collection.
        self._snapshot_all()
        self._admit_observed_cleanup_bounds(self._signed_observations)
        selected: dict[tuple[str, str], dict[str, Any]] = {}
        request_observations: list[dict[str, Any]] = []
        for envelope in self._signed_observations:
            observation = envelope.get("observation")
            if not isinstance(observation, Mapping):
                continue
            node_id = observation.get("node_id")
            event = observation.get("event")
            details = observation.get("details")
            if not isinstance(node_id, str) or not isinstance(event, str):
                continue
            if event in {"configured", "started", "snapshot"}:
                selected[(node_id, event)] = envelope
            elif (
                event in {"inference_started", "inference_decoded"}
                and isinstance(details, Mapping)
                and details.get("request_id") == request_id
            ):
                request_observations.append(envelope)
        required = {
            (node_id, event)
            for node_id in self._sessions
            for event in ("configured", "started", "snapshot")
        }
        if set(selected) != required or not request_observations:
            raise RuntimeError("live_attestation_observation_window_incomplete")
        signed_observations = [
            *(
                selected[(node_id, event)]
                for event in ("configured", "started")
                for node_id in sorted(self._sessions)
            ),
            *request_observations,
            *(selected[(node_id, "snapshot")] for node_id in sorted(self._sessions)),
        ]
        return {
            "protocol": "mycelium.live_route_attestation.v1",
            "captured_at_unix_ms": int(time.time() * 1_000),
            "run_id": self._plan["run_id"],
            "entry_node_id": self._request_entry_nodes.get(
                request_id, self._plan["entry_node_id"]
            ),
            "request_id": request_id,
            "prompt_token_ids": list(self._request_inputs[request_id]),
            "max_new_tokens": self._request_limits[request_id],
            "execution_graph": execution_graph_to_dict(self._graph),
            "output_token_ids": list(self._request_outputs[request_id]),
            "signed_observations": json.loads(json.dumps(signed_observations)),
            "counters": {
                "frames_sent": self.counters().frames_sent,
                "frames_received": self.counters().frames_received,
                "applied_operation_count": self.counters().applied_operation_count,
                "fatal": self.counters().fatal,
            },
        }

    def process_id(self, node_id: str) -> int:
        """Expose a verified worker PID for bounded physical fault injection."""
        with self._lock:
            identity = self._identities.get(node_id)
            process_id = None if identity is None else identity.get("process_id")
            if (
                not isinstance(process_id, int)
                or isinstance(process_id, bool)
                or process_id <= 0
            ):
                raise RuntimeError("peer_process_identity_unavailable")
            return process_id

    def diagnostic_stderr(self, node_id: str) -> str:
        """Return a bounded worker traceback for a failed qualification run."""

        with self._lock:
            session = self._sessions.get(node_id)
            if session is None:
                return ""
            return session.stderr.decode("utf-8", errors="replace")[-4_096:]

    def diagnostic_evidence(
        self,
        node_id: str,
        *,
        reason: str,
        timeout_seconds: float = 15.0,
    ) -> Mapping[str, Any] | None:
        """Ask one node for its evidence bundle over its control channel.

        Used only after a failed startup, before teardown.  The node writes the
        bundle beside its staging root and returns it; this is how a remote
        node's thread dump and transport state reach the operator.  ``None``
        when the node has no command process (managed/external sessions).
        """

        with self._lock:
            session = self._sessions.get(node_id)
        process = getattr(session, "_process", None)
        command = getattr(process, "command", None)
        if not callable(command):
            return None
        result = command(
            "diagnostic_evidence",
            {"reason": reason},
            timeout_seconds=timeout_seconds,
            terminate_on_timeout=False,
            command_id=self._command_id(node_id, "diagnostic-evidence"),
        )
        return result if isinstance(result, Mapping) else None

    def is_alive(self) -> bool:
        return (
            self._open
            and not self._closed
            and self._fatal is None
            and bool(self._sessions)
            and all(session.returncode is None for session in self._sessions.values())
        )

    def abort_open(self) -> None:
        """Abandon this route's open from another thread (owner shutdown).

        ``open`` holds the route lock for the whole node start-up, so ``close``
        from another thread would wait the start-up out. This takes no lock: it
        latches the refusal ``open`` checks before every node step and closes
        the node sessions started so far, so a pending node command fails at
        once and ``open`` closes the route itself. A route that already opened
        loses its sessions, so a qualification running on it fails at once.
        """

        self._open_aborted = True
        for session in tuple(self._sessions.values()):
            try:
                session.close()
            except BaseException:
                pass

    def _refuse_aborted_open(self) -> None:
        if self._open_aborted:
            raise ControllerError("route_open_aborted")

    def close(self) -> None:
        _unregister_open_route(self)
        self._liveness_monitor_stop.set()
        reconciliation_stop = getattr(self, "_cleanup_reconciliation_stop", None)
        if isinstance(reconciliation_stop, threading.Event):
            reconciliation_stop.set()
        reconciliation_executor = getattr(
            self, "_cleanup_reconciliation_executor", None
        )
        if reconciliation_executor is not None:
            reconciliation_executor.shutdown(wait=True, cancel_futures=True)
        monitor = self._liveness_monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=1.0)
        with self._lock:
            if self._closed:
                return
            for node_id, session in tuple(self._sessions.items()):
                if session.returncode is None:
                    try:
                        session.send(
                            command_id=self._command_id(node_id, "stop"),
                            command="stop",
                            payload={},
                        )
                    except BaseException:
                        pass
                try:
                    session.close()
                except BaseException:
                    pass
            self._open = False
            self._closed = True
            if self._seed_authority is not None:
                try:
                    self._seed_authority.close()
                finally:
                    self._seed_authority = None

    def cleanup(self) -> None:
        """Remove only digest-bound staging roots after every process is closed."""
        with self._lock:
            if self._open or any(
                session.returncode is None for session in self._sessions.values()
            ):
                raise RuntimeError("route_cleanup_requires_closed_processes")
            try:
                self._controller.execute("cleanup")
            except BaseException as exc:
                self._fatal = getattr(exc, "code", type(exc).__name__)
                raise


__all__ = [
    "FakeLiveRoute",
    "InferenceCancelled",
    "InferenceResult",
    "LiveRoute",
    "LiveSeedAuthority",
    "LiveSeedStateError",
    "PhysicalLiveRoute",
    "RouteCounters",
    "RouteIdentity",
    "TokenSink",
]
