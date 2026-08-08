"""SQLite lease coordinator for bounded physical qualification.

Each host has an atomic local authority. Complete path builds may replicate
fully validated reservation records between those authorities so cross-host
transport/runtime qualification does not silently substitute process-local
state. This remains intentionally narrower than a production reservation
transport.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from mycelium_router.contracts import (
    ExecutionGraph,
    PathBuildState,
    Placement,
    ReservationCommitResult,
    ReservationRequest,
    ReservationResult,
)
from mycelium_router.live_ports import (
    CapacityReservationSnapshot,
    CapacitySnapshot,
)
from mycelium_router.ports import CapacityPort, TopologyProvider
from mycelium_router.validation import validate_execution_graph


SQLITE_CAPACITY_CLAIM_BOUNDARY = (
    "path_carried_sqlite_replication_for_cross_host_physical_qualification_"
    "not_remote_production_reservation_transport"
)


class SQLiteQualificationCapacityPort(CapacityPort):
    claim_boundary = SQLITE_CAPACITY_CLAIM_BOUNDARY

    def __init__(
        self,
        database: Path,
        topology: TopologyProvider,
        node_available_kv_bytes: Mapping[str, int],
        *,
        clock,
        id_source,
        maximum_imported_lease_seconds: float,
    ) -> None:
        self._database = Path(database).resolve()
        self._database.parent.mkdir(parents=True, exist_ok=True)
        self._topology = topology
        self._clock = clock
        self._id_source = id_source
        if (
            isinstance(maximum_imported_lease_seconds, bool)
            or not isinstance(maximum_imported_lease_seconds, (int, float))
            or not math.isfinite(float(maximum_imported_lease_seconds))
            or maximum_imported_lease_seconds <= 0
        ):
            raise ValueError("invalid_maximum_imported_lease_seconds")
        self._maximum_imported_lease_seconds = float(
            maximum_imported_lease_seconds
        )
        self._capacities = dict(node_available_kv_bytes)
        graph = self._graph()
        expected_nodes = {
            placement.node_id
            for stage in graph.stages
            for placement in stage.placements
        }
        if set(self._capacities) != expected_nodes:
            raise ValueError("capacity_node_set_mismatch")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in self._capacities.values()
        ):
            raise ValueError("invalid_node_capacity")
        self._initialize(graph)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self, graph: ExecutionGraph) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS coordinator (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        deployment_id TEXT NOT NULL,
                        deployment_epoch INTEGER NOT NULL,
                        topology_version INTEGER NOT NULL,
                        manifest_digest TEXT NOT NULL,
                        capacities_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reservations (
                        reservation_id TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL,
                        path_id TEXT NOT NULL,
                        path_attempt INTEGER NOT NULL,
                        placement_id TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        placement_identity TEXT NOT NULL,
                        kv_bytes INTEGER NOT NULL,
                        deployment_epoch INTEGER NOT NULL,
                        lease_expires_at REAL NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('RESERVED', 'COMMITTED', 'RELEASED', 'EXPIRED')
                        ),
                        UNIQUE(request_id, path_attempt, placement_id)
                    )
                    """
                )
                capacities_json = json.dumps(
                    self._capacities, sort_keys=True, separators=(",", ":")
                )
                row = connection.execute(
                    "SELECT * FROM coordinator WHERE singleton = 1"
                ).fetchone()
                expected = (
                    graph.deployment_id,
                    graph.deployment_epoch,
                    graph.topology_version,
                    graph.manifest_digest,
                    capacities_json,
                )
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO coordinator VALUES (1, ?, ?, ?, ?, ?)
                        """,
                        expected,
                    )
                elif tuple(row[key] for key in row.keys()[1:]) != expected:
                    raise ValueError("capacity_coordinator_identity_mismatch")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def reserve(self, request: ReservationRequest) -> ReservationResult:
        invalid = self._validate_request(request)
        if invalid:
            return ReservationResult(False, reason=invalid)
        graph = self._graph()
        if request.deployment_epoch != graph.deployment_epoch:
            return ReservationResult(False, reason="deployment_epoch_mismatch")
        placement = self._placements(graph).get(request.placement_id)
        if placement is None:
            return ReservationResult(False, reason="unknown_placement")
        now = self._now()
        if request.lease_expires_at <= now:
            return ReservationResult(False, reason="reservation_expired")
        placement_identity = self._placement_identity(placement)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._reap(connection, now)
                row = connection.execute(
                    """
                    SELECT * FROM reservations
                    WHERE request_id = ? AND path_attempt = ? AND placement_id = ?
                    """,
                    (request.request_id, request.path_attempt, request.placement_id),
                ).fetchone()
                if row is not None:
                    result = self._existing_result(
                        row, request, placement_identity, now
                    )
                    connection.execute("COMMIT")
                    return result
                charged = connection.execute(
                    """
                    SELECT COALESCE(SUM(kv_bytes), 0) AS charged
                    FROM reservations
                    WHERE node_id = ? AND status IN ('RESERVED', 'COMMITTED')
                    """,
                    (placement.node_id,),
                ).fetchone()["charged"]
                if charged + request.kv_bytes > self._capacities[placement.node_id]:
                    connection.execute("COMMIT")
                    return ReservationResult(False, reason="capacity_exceeded")
                reservation_id = self._id_source.new("reservation")
                if not isinstance(reservation_id, str) or not reservation_id:
                    raise RuntimeError("invalid_reservation_id")
                connection.execute(
                    """
                    INSERT INTO reservations (
                        reservation_id, request_id, path_id, path_attempt,
                        placement_id, node_id, placement_identity, kv_bytes,
                        deployment_epoch, lease_expires_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED')
                    """,
                    (
                        reservation_id,
                        request.request_id,
                        request.path_id,
                        request.path_attempt,
                        request.placement_id,
                        placement.node_id,
                        placement_identity,
                        request.kv_bytes,
                        request.deployment_epoch,
                        request.lease_expires_at,
                    ),
                )
                connection.execute("COMMIT")
                return ReservationResult(
                    True,
                    reservation_id=reservation_id,
                    deployment_epoch=request.deployment_epoch,
                    expires_at=request.lease_expires_at,
                )
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def commit(
        self,
        reservation_ids: tuple[str, ...],
        *,
        deployment_epoch: int,
    ) -> ReservationCommitResult:
        graph = self._graph()
        if (
            isinstance(deployment_epoch, bool)
            or not isinstance(deployment_epoch, int)
            or deployment_epoch < 0
        ):
            return ReservationCommitResult(False, "invalid_deployment_epoch")
        if deployment_epoch != graph.deployment_epoch:
            return ReservationCommitResult(False, "deployment_epoch_mismatch")
        if not reservation_ids:
            return ReservationCommitResult(False, "empty_reservation_set")
        placements = self._placements(graph)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._reap(connection, now)
                rows = []
                for reservation_id in reservation_ids:
                    row = connection.execute(
                        "SELECT * FROM reservations WHERE reservation_id = ?",
                        (reservation_id,),
                    ).fetchone()
                    if row is None:
                        connection.execute("COMMIT")
                        return ReservationCommitResult(False, "unknown_reservation")
                    if row["status"] == "RELEASED":
                        connection.execute("COMMIT")
                        return ReservationCommitResult(False, "reservation_released")
                    if row["status"] == "EXPIRED":
                        connection.execute("COMMIT")
                        return ReservationCommitResult(False, "reservation_expired")
                    if row["deployment_epoch"] != deployment_epoch:
                        connection.execute("COMMIT")
                        return ReservationCommitResult(
                            False, "deployment_epoch_mismatch"
                        )
                    placement = placements.get(row["placement_id"])
                    if placement is None:
                        connection.execute("COMMIT")
                        return ReservationCommitResult(False, "unknown_placement")
                    if self._placement_identity(placement) != row["placement_identity"]:
                        connection.execute("COMMIT")
                        return ReservationCommitResult(False, "placement_changed")
                    rows.append(row)
                connection.executemany(
                    """
                    UPDATE reservations SET status = 'COMMITTED'
                    WHERE reservation_id = ? AND status = 'RESERVED'
                    """,
                    ((row["reservation_id"],) for row in rows),
                )
                connection.execute("COMMIT")
                return ReservationCommitResult(True)
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def synchronize_build(
        self,
        build: PathBuildState,
    ) -> ReservationCommitResult:
        """Atomically mirror path-carried reservations into this host's DB.

        Physical qualification uses one SQLite authority per host. A complete
        build can therefore arrive with reservation IDs minted by its peer.
        Mirror only records whose full path, placement, lease, and capacity
        charge can be independently reconstructed and validated.
        """

        if not isinstance(build, PathBuildState):
            return ReservationCommitResult(False, "invalid_path_build")
        graph = self._graph()
        if build.graph != graph:
            return ReservationCommitResult(False, "capacity_graph_mismatch")
        if len(build.ordered_hops) != len(graph.stages):
            return ReservationCommitResult(False, "path_incomplete")
        if len({hop.reservation_id for hop in build.ordered_hops}) != len(
            build.ordered_hops
        ):
            return ReservationCommitResult(False, "duplicate_reservation_id")

        now = self._now()
        records: list[tuple[str, ReservationRequest, Placement, str]] = []
        for stage, hop in zip(graph.stages, build.ordered_hops, strict=True):
            placement = next(
                (
                    candidate
                    for candidate in stage.placements
                    if candidate.placement_id == hop.placement_id
                ),
                None,
            )
            if placement is None or hop.stage_id != stage.stage_id:
                return ReservationCommitResult(False, "placement_stage_mismatch")
            if not isinstance(hop.reservation_id, str) or not hop.reservation_id:
                return ReservationCommitResult(False, "invalid_reservation_id")
            request = ReservationRequest(
                request_id=build.request.request_id,
                path_id=build.path_id,
                path_attempt=build.path_attempt,
                placement_id=hop.placement_id,
                kv_bytes=(
                    len(build.request.prompt_token_ids) + build.request.max_new_tokens
                )
                * stage.stage_cost.kv_bytes_per_context_token,
                deployment_epoch=hop.reservation_epoch,
                lease_expires_at=hop.reservation_expires_at,
            )
            invalid = self._validate_request(request)
            if invalid:
                return ReservationCommitResult(False, invalid)
            if request.deployment_epoch != graph.deployment_epoch:
                return ReservationCommitResult(False, "deployment_epoch_mismatch")
            if request.lease_expires_at <= now:
                return ReservationCommitResult(False, "reservation_expired")
            if (
                request.lease_expires_at - now
                > self._maximum_imported_lease_seconds
            ):
                return ReservationCommitResult(False, "lease_duration_exceeded")
            records.append(
                (
                    hop.reservation_id,
                    request,
                    placement,
                    self._placement_identity(placement),
                )
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._reap(connection, now)
                for reservation_id, request, placement, placement_identity in records:
                    row = connection.execute(
                        "SELECT * FROM reservations WHERE reservation_id = ?",
                        (reservation_id,),
                    ).fetchone()
                    if row is not None:
                        existing = self._existing_result(
                            row,
                            request,
                            placement_identity,
                            now,
                        )
                        if not existing.accepted:
                            connection.execute("ROLLBACK")
                            return ReservationCommitResult(False, existing.reason)
                        continue
                    conflicting = connection.execute(
                        """
                        SELECT reservation_id FROM reservations
                        WHERE request_id = ? AND path_attempt = ? AND placement_id = ?
                        """,
                        (
                            request.request_id,
                            request.path_attempt,
                            request.placement_id,
                        ),
                    ).fetchone()
                    if conflicting is not None:
                        connection.execute("ROLLBACK")
                        return ReservationCommitResult(False, "idempotency_conflict")
                    charged = connection.execute(
                        """
                        SELECT COALESCE(SUM(kv_bytes), 0) AS charged
                        FROM reservations
                        WHERE node_id = ? AND status IN ('RESERVED', 'COMMITTED')
                        """,
                        (placement.node_id,),
                    ).fetchone()["charged"]
                    if charged + request.kv_bytes > self._capacities[placement.node_id]:
                        connection.execute("ROLLBACK")
                        return ReservationCommitResult(False, "capacity_exceeded")
                    connection.execute(
                        """
                        INSERT INTO reservations (
                            reservation_id, request_id, path_id, path_attempt,
                            placement_id, node_id, placement_identity, kv_bytes,
                            deployment_epoch, lease_expires_at, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED')
                        """,
                        (
                            reservation_id,
                            request.request_id,
                            request.path_id,
                            request.path_attempt,
                            request.placement_id,
                            placement.node_id,
                            placement_identity,
                            request.kv_bytes,
                            request.deployment_epoch,
                            request.lease_expires_at,
                        ),
                    )
                connection.execute("COMMIT")
                return ReservationCommitResult(True)
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def release(self, reservation_ids: tuple[str, ...]) -> None:
        if not reservation_ids:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    """
                    UPDATE reservations SET status = 'RELEASED'
                    WHERE reservation_id = ?
                      AND status IN ('RESERVED', 'COMMITTED')
                    """,
                    ((reservation_id,) for reservation_id in reservation_ids),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def snapshot(self) -> CapacitySnapshot:
        graph = self._graph()
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._reap(connection, now)
                rows = connection.execute(
                    "SELECT * FROM reservations ORDER BY reservation_id"
                ).fetchall()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        reserved = {node_id: 0 for node_id in self._capacities}
        reservations = {}
        for row in rows:
            if row["status"] in {"RESERVED", "COMMITTED"}:
                reserved[row["node_id"]] += row["kv_bytes"]
            reservations[row["reservation_id"]] = CapacityReservationSnapshot(
                reservation_id=row["reservation_id"],
                request_id=row["request_id"],
                path_id=row["path_id"],
                path_attempt=row["path_attempt"],
                placement_id=row["placement_id"],
                node_id=row["node_id"],
                kv_bytes=row["kv_bytes"],
                deployment_epoch=row["deployment_epoch"],
                lease_expires_at=row["lease_expires_at"],
                status=row["status"],
            )
        available = {
            node_id: self._capacities[node_id] - reserved[node_id]
            for node_id in self._capacities
        }
        return CapacitySnapshot(
            claim_boundary=self.claim_boundary,
            deployment_id=graph.deployment_id,
            deployment_epoch=graph.deployment_epoch,
            topology_version=graph.topology_version,
            observed_at=now,
            node_capacity_kv_bytes=MappingProxyType(dict(self._capacities)),
            node_reserved_kv_bytes=MappingProxyType(reserved),
            node_available_kv_bytes=MappingProxyType(available),
            reservations=MappingProxyType(reservations),
        )

    @staticmethod
    def _validate_request(request: ReservationRequest) -> str:
        if not isinstance(request, ReservationRequest):
            return "invalid_reservation_request"
        if not all(
            isinstance(value, str) and value
            for value in (request.request_id, request.path_id, request.placement_id)
        ):
            return "invalid_reservation_identity"
        if (
            isinstance(request.path_attempt, bool)
            or not isinstance(request.path_attempt, int)
            or request.path_attempt < 0
        ):
            return "invalid_path_attempt"
        if (
            isinstance(request.kv_bytes, bool)
            or not isinstance(request.kv_bytes, int)
            or request.kv_bytes < 0
        ):
            return "invalid_kv_bytes"
        if (
            isinstance(request.deployment_epoch, bool)
            or not isinstance(request.deployment_epoch, int)
            or request.deployment_epoch < 0
        ):
            return "invalid_deployment_epoch"
        if (
            isinstance(request.lease_expires_at, bool)
            or not isinstance(request.lease_expires_at, (int, float))
            or not math.isfinite(float(request.lease_expires_at))
        ):
            return "invalid_lease_expiry"
        return ""

    @staticmethod
    def _placement_identity(placement: Placement) -> str:
        return json.dumps(
            [
                placement.placement_id,
                placement.node_id,
                placement.replica_group_id,
                placement.assignment_id,
                placement.stage_signature,
                placement.load_proof_digest,
                placement.runtime_backend,
                placement.runtime_endpoint,
                placement.lifecycle_state,
            ],
            separators=(",", ":"),
        )

    @staticmethod
    def _placements(graph: ExecutionGraph) -> dict[str, Placement]:
        return {
            placement.placement_id: placement
            for stage in graph.stages
            for placement in stage.placements
        }

    def _graph(self) -> ExecutionGraph:
        graph = self._topology.snapshot()
        validate_execution_graph(graph)
        return graph

    def _now(self) -> float:
        value = self._clock.now()
        if not isinstance(value, (int, float)):
            raise ValueError("invalid_clock_value")
        return float(value)

    @staticmethod
    def _reap(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            """
            UPDATE reservations SET status = 'EXPIRED'
            WHERE status = 'RESERVED' AND lease_expires_at <= ?
            """,
            (now,),
        )

    @staticmethod
    def _existing_result(
        row: sqlite3.Row,
        request: ReservationRequest,
        placement_identity: str,
        now: float,
    ) -> ReservationResult:
        expected = (
            request.path_id,
            request.kv_bytes,
            request.deployment_epoch,
            request.lease_expires_at,
        )
        observed = (
            row["path_id"],
            row["kv_bytes"],
            row["deployment_epoch"],
            row["lease_expires_at"],
        )
        if expected != observed:
            return ReservationResult(False, reason="idempotency_conflict")
        if row["placement_identity"] != placement_identity:
            return ReservationResult(False, reason="placement_changed")
        if row["status"] == "RELEASED":
            return ReservationResult(False, reason="reservation_released")
        if row["status"] == "EXPIRED":
            return ReservationResult(False, reason="reservation_expired")
        if row["status"] == "COMMITTED" and row["lease_expires_at"] <= now:
            return ReservationResult(
                False, reason="reservation_already_committed"
            )
        return ReservationResult(
            True,
            reservation_id=row["reservation_id"],
            deployment_epoch=row["deployment_epoch"],
            expires_at=row["lease_expires_at"],
        )
