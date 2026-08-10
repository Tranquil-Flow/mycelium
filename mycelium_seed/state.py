# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable SQLite state for the seed coordinator."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator

from mycelium_invite import SqliteInviteRegistry
from mycelium_membership import (
    HEARTBEAT_PROTOCOL,
    LEASE_RENEWAL_PROTOCOL,
    MembershipContractError,
    verify_membership_message,
)
from mycelium_qualification.evidence import canonical_json_bytes


_SCHEMA_VERSION = 8
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _sqlite_state_error_code(exc: sqlite3.Error) -> str:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int) and error_code & 0xFF in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
    }:
        return "seed_state_corrupt"
    return "seed_state_unavailable"


class SeedStateError(RuntimeError):
    """Stable durable-state error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SqliteSeedState:
    """Persist members, replay IDs, emitted IDs, and assignment outcomes."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        # Reuse the invitation registry's fail-closed path preparation and keep
        # both first-contact replay and seed state in one protected database.
        SqliteInviteRegistry(self.database)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS seed_metadata (
                    key TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS seed_members (
                    node_id TEXT PRIMARY KEY NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    endpoint_addrs_json TEXT NOT NULL,
                    peer_class TEXT NOT NULL,
                    runtime_capability_json TEXT NOT NULL,
                    verification_key_digest TEXT NOT NULL,
                    incarnation TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    lease_expires_at REAL NOT NULL,
                    last_heartbeat_sequence INTEGER NOT NULL
                        CHECK (last_heartbeat_sequence >= 0),
                    last_liveness_at REAL NOT NULL,
                    next_heartbeat_due_at REAL NOT NULL,
                    last_activity_receipt_at REAL,
                    active_requests INTEGER NOT NULL CHECK (active_requests >= 0),
                    lifecycle_state TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS seed_replay (
                    node_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    message_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (node_id, generation, message_id),
                    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
                        ON DELETE CASCADE
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS seed_emitted_messages (
                    message_id TEXT PRIMARY KEY NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS seed_join_acceptances (
                    nonce TEXT PRIMARY KEY NOT NULL,
                    invite_token_digest TEXT NOT NULL,
                    request_envelope_digest TEXT NOT NULL,
                    request_message_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    verification_key_digest TEXT NOT NULL,
                    incarnation TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    acceptance_json TEXT NOT NULL,
                    FOREIGN KEY (nonce) REFERENCES consumed_invites(nonce),
                    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS seed_heartbeat_renewals (
                    node_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    heartbeat_message_id TEXT NOT NULL,
                    request_envelope_digest TEXT NOT NULL,
                    heartbeat_sequence INTEGER NOT NULL
                        CHECK (heartbeat_sequence >= 1),
                    heartbeat_json TEXT NOT NULL,
                    renewal_message_id TEXT NOT NULL UNIQUE,
                    renewal_json TEXT NOT NULL,
                    PRIMARY KEY (node_id, generation, heartbeat_message_id),
                    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (renewal_message_id)
                        REFERENCES seed_emitted_messages(message_id)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS seed_assignments (
                    assignment_id TEXT PRIMARY KEY NOT NULL,
                    node_id TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    deployment_epoch INTEGER NOT NULL CHECK (deployment_epoch >= 1),
                    membership_generation INTEGER NOT NULL
                        CHECK (membership_generation >= 1),
                    accepted INTEGER CHECK (accepted IN (0, 1) OR accepted IS NULL),
                    result_code TEXT,
                    load_proof_digest TEXT,
                    runtime_endpoint TEXT,
                    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS seed_authority_rotations (
                    authority_generation INTEGER PRIMARY KEY NOT NULL
                        CHECK (authority_generation >= 2),
                    previous_generation INTEGER NOT NULL
                        CHECK (previous_generation >= 1),
                    old_seed_key_digest TEXT NOT NULL,
                    new_seed_key_digest TEXT NOT NULL,
                    initiated_at REAL NOT NULL,
                    effective_at REAL NOT NULL,
                    overlap_expires_at REAL NOT NULL,
                    reason TEXT NOT NULL,
                    transition_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMPLETED'))
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS seed_rotation_acknowledgements (
                    node_id TEXT NOT NULL,
                    member_generation INTEGER NOT NULL
                        CHECK (member_generation >= 1),
                    authority_generation INTEGER NOT NULL
                        CHECK (authority_generation >= 2),
                    transition_digest TEXT NOT NULL,
                    message_id TEXT NOT NULL UNIQUE,
                    acknowledged_at REAL NOT NULL,
                    PRIMARY KEY (node_id, authority_generation),
                    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (authority_generation)
                        REFERENCES seed_authority_rotations(authority_generation)
                        ON DELETE CASCADE
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS seed_resume_acceptances (
                    request_message_id TEXT PRIMARY KEY NOT NULL,
                    request_envelope_digest TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    previous_incarnation TEXT NOT NULL,
                    accepted_incarnation TEXT NOT NULL,
                    previous_generation INTEGER NOT NULL
                        CHECK (previous_generation >= 1),
                    generation INTEGER NOT NULL CHECK (generation >= 2),
                    acceptance_json TEXT NOT NULL,
                    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
                        ON DELETE CASCADE
                ) WITHOUT ROWID;
                """
            )
            row = connection.execute(
                "SELECT value FROM seed_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO seed_metadata (key, value) VALUES ('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            elif row["value"] in {"1", "2", "3", "4", "5", "6", "7"}:
                columns = {
                    item["name"]
                    for item in connection.execute("PRAGMA table_info(seed_members)")
                }
                if "last_heartbeat_sequence" not in columns:
                    connection.execute(
                        "ALTER TABLE seed_members ADD COLUMN "
                        "last_heartbeat_sequence INTEGER NOT NULL DEFAULT 0"
                    )
                if "peer_class" not in columns:
                    connection.execute(
                        "ALTER TABLE seed_members ADD COLUMN peer_class TEXT "
                        "NOT NULL DEFAULT 'linux_tbd'"
                    )
                if "runtime_capability_json" not in columns:
                    connection.execute(
                        "ALTER TABLE seed_members ADD COLUMN runtime_capability_json TEXT "
                        "NOT NULL DEFAULT '{\"activation_protocol\":null,"
                        "\"runtime_backend\":\"tbd\",\"transport\":\"none\"}'"
                    )
                if "last_liveness_at" not in columns:
                    connection.execute(
                        "ALTER TABLE seed_members ADD COLUMN "
                        "last_liveness_at REAL NOT NULL DEFAULT 0"
                    )
                if "next_heartbeat_due_at" not in columns:
                    connection.execute(
                        "ALTER TABLE seed_members ADD COLUMN "
                        "next_heartbeat_due_at REAL NOT NULL DEFAULT 0"
                    )
                if "last_activity_receipt_at" not in columns:
                    connection.execute(
                        "ALTER TABLE seed_members ADD COLUMN last_activity_receipt_at REAL"
                    )
                if "active_requests" not in columns:
                    connection.execute(
                        "ALTER TABLE seed_members ADD COLUMN "
                        "active_requests INTEGER NOT NULL DEFAULT 0"
                    )
                if "lifecycle_state" not in columns:
                    connection.execute(
                        "ALTER TABLE seed_members ADD COLUMN "
                        "lifecycle_state TEXT NOT NULL DEFAULT 'NEW'"
                    )
                connection.execute(
                    "UPDATE seed_metadata SET value = ? WHERE key = 'schema_version'",
                    (str(_SCHEMA_VERSION),),
                )
            elif row["value"] != str(_SCHEMA_VERSION):
                raise SeedStateError("seed_state_schema_unsupported")
            self._validate_heartbeat_renewal_schema(connection)
            connection.commit()
        except SeedStateError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def identity_binding(self) -> dict[str, str]:
        """Return the exact durable coordinator binding without creating it."""

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT key, value FROM seed_metadata "
                "WHERE key IN ('swarm_id', 'seed_node_id', 'seed_key_digest')"
            ).fetchall()
            binding = {
                str(row["key"]): str(row["value"])
                for row in rows
            }
            if set(binding) != {"swarm_id", "seed_node_id", "seed_key_digest"}:
                raise SeedStateError("seed_state_identity_missing")
            return binding
        except SeedStateError:
            raise
        except sqlite3.Error as exc:
            raise SeedStateError(_sqlite_state_error_code(exc)) from exc
        finally:
            connection.close()

    def authority_state(self) -> dict[str, Any]:
        """Return the current authority generation and latest rotation, if any."""

        connection = self._connect()
        try:
            generation_row = connection.execute(
                "SELECT value FROM seed_metadata WHERE key = 'authority_generation'"
            ).fetchone()
            if generation_row is None:
                raise SeedStateError("seed_state_identity_missing")
            try:
                generation = int(generation_row["value"])
            except (TypeError, ValueError) as exc:
                raise SeedStateError("seed_state_corrupt") from exc
            if generation < 1 or str(generation) != generation_row["value"]:
                raise SeedStateError("seed_state_corrupt")
            row = connection.execute(
                "SELECT authority_generation, previous_generation, "
                "old_seed_key_digest, new_seed_key_digest, initiated_at, "
                "effective_at, overlap_expires_at, reason, transition_json, status "
                "FROM seed_authority_rotations ORDER BY authority_generation DESC LIMIT 1"
            ).fetchone()
            rotation = None if row is None else {
                "authority_generation": row["authority_generation"],
                "previous_generation": row["previous_generation"],
                "old_seed_key_digest": row["old_seed_key_digest"],
                "new_seed_key_digest": row["new_seed_key_digest"],
                "initiated_at": row["initiated_at"],
                "effective_at": row["effective_at"],
                "overlap_expires_at": row["overlap_expires_at"],
                "reason": row["reason"],
                "transition": json.loads(row["transition_json"]),
                "status": row["status"],
            }
            return {"authority_generation": generation, "rotation": rotation}
        except SeedStateError:
            raise
        except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as exc:
            raise SeedStateError("seed_state_corrupt") from exc
        finally:
            connection.close()

    def begin_authority_rotation(self, transition: Mapping[str, Any]) -> None:
        """Persist one monotonic pending authority transition."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            generation_row = connection.execute(
                "SELECT value FROM seed_metadata WHERE key = 'authority_generation'"
            ).fetchone()
            digest_row = connection.execute(
                "SELECT value FROM seed_metadata WHERE key = 'seed_key_digest'"
            ).fetchone()
            if generation_row is None or digest_row is None:
                raise SeedStateError("seed_state_identity_missing")
            current_generation = int(generation_row["value"])
            if (
                transition.get("previous_generation") != current_generation
                or transition.get("authority_generation") != current_generation + 1
                or transition.get("old_seed_key_digest") != digest_row["value"]
            ):
                raise SeedStateError("seed_authority_rotation_stale")
            pending = connection.execute(
                "SELECT 1 FROM seed_authority_rotations WHERE status = 'PENDING'"
            ).fetchone()
            if pending is not None:
                raise SeedStateError("seed_authority_rotation_pending")
            connection.execute(
                "INSERT INTO seed_authority_rotations "
                "(authority_generation, previous_generation, old_seed_key_digest, "
                "new_seed_key_digest, initiated_at, effective_at, overlap_expires_at, "
                "reason, transition_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')",
                (
                    transition["authority_generation"],
                    transition["previous_generation"],
                    transition["old_seed_key_digest"],
                    transition["new_seed_key_digest"],
                    transition["initiated_at"],
                    transition["effective_at"],
                    transition["overlap_expires_at"],
                    transition["reason"],
                    canonical_json_bytes(dict(transition)).decode("utf-8"),
                ),
            )
            connection.commit()
        except SeedStateError:
            connection.rollback()
            raise
        except (KeyError, sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def complete_authority_rotation(
        self,
        *,
        authority_generation: int,
        new_seed_key_digest: str,
    ) -> None:
        """Atomically promote one pending transition in durable metadata."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT previous_generation, new_seed_key_digest, status "
                "FROM seed_authority_rotations WHERE authority_generation = ?",
                (authority_generation,),
            ).fetchone()
            generation_row = connection.execute(
                "SELECT value FROM seed_metadata WHERE key = 'authority_generation'"
            ).fetchone()
            digest_row = connection.execute(
                "SELECT value FROM seed_metadata WHERE key = 'seed_key_digest'"
            ).fetchone()
            if row is None or generation_row is None or digest_row is None:
                raise SeedStateError("seed_authority_rotation_unknown")
            if (
                row["new_seed_key_digest"] != new_seed_key_digest
                or row["previous_generation"] + 1 != authority_generation
            ):
                raise SeedStateError("seed_authority_rotation_stale")
            current_generation = int(generation_row["value"])
            if row["status"] == "COMPLETED":
                if (
                    current_generation != authority_generation
                    or digest_row["value"] != new_seed_key_digest
                ):
                    raise SeedStateError("seed_state_corrupt")
                connection.commit()
                return
            if (
                row["status"] != "PENDING"
                or current_generation != row["previous_generation"]
            ):
                raise SeedStateError("seed_authority_rotation_stale")
            connection.execute(
                "UPDATE seed_metadata SET value = ? WHERE key = 'seed_key_digest'",
                (new_seed_key_digest,),
            )
            connection.execute(
                "UPDATE seed_metadata SET value = ? WHERE key = 'authority_generation'",
                (str(authority_generation),),
            )
            connection.execute(
                "UPDATE seed_authority_rotations SET status = 'COMPLETED' "
                "WHERE authority_generation = ?",
                (authority_generation,),
            )
            connection.commit()
        except SeedStateError:
            connection.rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def save_seed_rotation_acknowledgement(
        self,
        message: Mapping[str, Any],
    ) -> None:
        """Persist a current member's acknowledgement of the exact pending transition."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rotation = connection.execute(
                "SELECT transition_json, status FROM seed_authority_rotations "
                "WHERE authority_generation = ?",
                (message["authority_generation"],),
            ).fetchone()
            member = connection.execute(
                "SELECT generation, incarnation FROM seed_members WHERE node_id = ?",
                (message["sender_node_id"],),
            ).fetchone()
            if rotation is None or rotation["status"] != "PENDING":
                raise SeedStateError("seed_authority_rotation_unknown")
            if (
                member is None
                or member["generation"] != message["generation"]
                or member["incarnation"] != message["incarnation"]
            ):
                raise SeedStateError("seed_member_generation_stale")
            expected_digest = "sha256:" + hashlib.sha256(
                rotation["transition_json"].encode("utf-8")
            ).hexdigest()
            if message["transition_digest"] != expected_digest:
                raise SeedStateError("seed_authority_rotation_ack_mismatch")
            connection.execute(
                "INSERT INTO seed_rotation_acknowledgements "
                "(node_id, member_generation, authority_generation, "
                "transition_digest, message_id, acknowledged_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(node_id, authority_generation) DO UPDATE SET "
                "member_generation = excluded.member_generation, "
                "transition_digest = excluded.transition_digest, "
                "message_id = excluded.message_id, "
                "acknowledged_at = excluded.acknowledged_at",
                (
                    message["sender_node_id"],
                    message["generation"],
                    message["authority_generation"],
                    message["transition_digest"],
                    message["message_id"],
                    message["issued_at"],
                ),
            )
            connection.commit()
        except SeedStateError:
            connection.rollback()
            raise
        except (KeyError, sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def seed_rotation_acknowledgements(
        self,
        *,
        authority_generation: int,
    ) -> list[dict[str, Any]]:
        """Load durable acknowledgements for one authority generation."""

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT node_id, member_generation, authority_generation, "
                "transition_digest, message_id, acknowledged_at "
                "FROM seed_rotation_acknowledgements "
                "WHERE authority_generation = ? ORDER BY node_id",
                (authority_generation,),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise SeedStateError("seed_state_corrupt") from exc
        finally:
            connection.close()

    @staticmethod
    def _validate_heartbeat_renewal_schema(
        connection: sqlite3.Connection,
    ) -> None:
        """Reject any current-version renewal table outside the exact contract."""

        expected_columns = (
            (0, "node_id", "TEXT", 1, None, 1),
            (1, "generation", "INTEGER", 1, None, 2),
            (2, "heartbeat_message_id", "TEXT", 1, None, 3),
            (3, "request_envelope_digest", "TEXT", 1, None, 0),
            (4, "heartbeat_sequence", "INTEGER", 1, None, 0),
            (5, "heartbeat_json", "TEXT", 1, None, 0),
            (6, "renewal_message_id", "TEXT", 1, None, 0),
            (7, "renewal_json", "TEXT", 1, None, 0),
        )
        table_info = tuple(
            tuple(row)
            for row in connection.execute(
                "PRAGMA table_info(seed_heartbeat_renewals)"
            )
        )
        table_xinfo = tuple(
            tuple(row)
            for row in connection.execute(
                "PRAGMA table_xinfo(seed_heartbeat_renewals)"
            )
        )
        if table_info != expected_columns or table_xinfo != tuple(
            (*column, 0) for column in expected_columns
        ):
            raise SeedStateError("seed_state_corrupt")

        indexes: set[tuple[int, str, int, tuple[str, ...]]] = set()
        for row in connection.execute(
            "PRAGMA index_list(seed_heartbeat_renewals)"
        ):
            index_name = row["name"].replace('"', '""')
            index_columns = tuple(
                item["name"]
                for item in connection.execute(
                    f'PRAGMA index_info("{index_name}")'
                )
            )
            indexes.add(
                (
                    int(row["unique"]),
                    row["origin"],
                    int(row["partial"]),
                    index_columns,
                )
            )
        expected_indexes = {
            (
                1,
                "pk",
                0,
                ("node_id", "generation", "heartbeat_message_id"),
            ),
            (1, "u", 0, ("renewal_message_id",)),
        }
        if indexes != expected_indexes:
            raise SeedStateError("seed_state_corrupt")

        foreign_keys = {
            (
                row["table"],
                row["from"],
                row["to"],
                row["on_update"],
                row["on_delete"],
                row["match"],
            )
            for row in connection.execute(
                "PRAGMA foreign_key_list(seed_heartbeat_renewals)"
            )
        }
        if foreign_keys != {
            (
                "seed_members",
                "node_id",
                "node_id",
                "NO ACTION",
                "CASCADE",
                "NONE",
            ),
            (
                "seed_emitted_messages",
                "renewal_message_id",
                "message_id",
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
        }:
            raise SeedStateError("seed_state_corrupt")

        schema_row = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'seed_heartbeat_renewals'
            """
        ).fetchone()
        if schema_row is None or not isinstance(schema_row["sql"], str):
            raise SeedStateError("seed_state_corrupt")
        normalized_sql = re.sub(r"\s+", "", schema_row["sql"].lower())
        expected_declarations = (
            "node_idtextnotnull,",
            "generationintegernotnullcheck(generation>=1),",
            "heartbeat_message_idtextnotnull,",
            "request_envelope_digesttextnotnull,",
            (
                "heartbeat_sequenceintegernotnull"
                "check(heartbeat_sequence>=1),"
            ),
            "heartbeat_jsontextnotnull,",
            "renewal_message_idtextnotnullunique,",
            "renewal_jsontextnotnull,",
        )
        if (
            not normalized_sql.endswith(")withoutrowid")
            or normalized_sql.count("check(") != 2
            or "check(generation>=1)" not in normalized_sql
            or "check(heartbeat_sequence>=1)" not in normalized_sql
            or any(
                declaration not in normalized_sql
                for declaration in expected_declarations
            )
        ):
            raise SeedStateError("seed_state_corrupt")

    def bind_identity(
        self,
        *,
        swarm_id: str,
        seed_node_id: str,
        seed_key_digest: str,
    ) -> None:
        expected = {
            "swarm_id": swarm_id,
            "seed_node_id": seed_node_id,
            "seed_key_digest": seed_key_digest,
        }
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for key, value in expected.items():
                row = connection.execute(
                    "SELECT value FROM seed_metadata WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO seed_metadata (key, value) VALUES (?, ?)",
                        (key, value),
                    )
                elif row["value"] != value:
                    raise SeedStateError("seed_state_identity_mismatch")
            generation = connection.execute(
                "SELECT value FROM seed_metadata WHERE key = 'authority_generation'"
            ).fetchone()
            if generation is None:
                connection.execute(
                    "INSERT INTO seed_metadata (key, value) VALUES "
                    "('authority_generation', '1')"
                )
            elif generation["value"] != str(int(generation["value"])) or int(
                generation["value"]
            ) < 1:
                raise SeedStateError("seed_state_corrupt")
            connection.commit()
        except SeedStateError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def load_members(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT node_id, endpoint_id, endpoint_addrs_json,
                       peer_class, runtime_capability_json,
                       verification_key_digest, incarnation, generation,
                       lease_expires_at, last_heartbeat_sequence,
                       last_liveness_at, next_heartbeat_due_at,
                       last_activity_receipt_at, active_requests, lifecycle_state
                FROM seed_members
                """
            ).fetchall()
            return [self._decode_member_row(row) for row in rows]
        except SeedStateError:
            raise
        except sqlite3.Error as exc:
            raise SeedStateError("seed_state_corrupt") from exc
        finally:
            connection.close()

    @staticmethod
    def _decode_member_row(row: sqlite3.Row) -> dict[str, Any]:
        """Strictly decode one persisted member without SQLite coercion."""

        try:
            text_fields = (
                "node_id",
                "endpoint_id",
                "peer_class",
                "verification_key_digest",
                "incarnation",
                "lifecycle_state",
            )
            if any(
                not isinstance(row[field], str) or not row[field]
                for field in text_fields
            ):
                raise SeedStateError("seed_state_corrupt")

            endpoint_raw_text = row["endpoint_addrs_json"]
            runtime_raw_text = row["runtime_capability_json"]
            if not isinstance(endpoint_raw_text, str) or not isinstance(
                runtime_raw_text, str
            ):
                raise SeedStateError("seed_state_corrupt")
            endpoint_raw = endpoint_raw_text.encode("utf-8")
            runtime_raw = runtime_raw_text.encode("utf-8")
            addresses = json.loads(endpoint_raw)
            runtime_capability = json.loads(runtime_raw)
            if (
                not isinstance(addresses, list)
                or not addresses
                or not all(
                    isinstance(value, str) and value for value in addresses
                )
                or canonical_json_bytes(addresses) != endpoint_raw
                or not isinstance(runtime_capability, dict)
                or canonical_json_bytes(runtime_capability) != runtime_raw
            ):
                raise SeedStateError("seed_state_corrupt")

            generation = row["generation"]
            heartbeat_sequence = row["last_heartbeat_sequence"]
            active_requests = row["active_requests"]
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 1
                or isinstance(heartbeat_sequence, bool)
                or not isinstance(heartbeat_sequence, int)
                or heartbeat_sequence < 0
                or isinstance(active_requests, bool)
                or not isinstance(active_requests, int)
                or active_requests < 0
            ):
                raise SeedStateError("seed_state_corrupt")

            numeric_fields = (
                "lease_expires_at",
                "last_liveness_at",
                "next_heartbeat_due_at",
            )
            if any(
                isinstance(row[field], bool)
                or not isinstance(row[field], (int, float))
                or not math.isfinite(float(row[field]))
                for field in numeric_fields
            ):
                raise SeedStateError("seed_state_corrupt")
            activity_receipt = row["last_activity_receipt_at"]
            if activity_receipt is not None and (
                isinstance(activity_receipt, bool)
                or not isinstance(activity_receipt, (int, float))
                or not math.isfinite(float(activity_receipt))
            ):
                raise SeedStateError("seed_state_corrupt")

            return {
                "node_id": row["node_id"],
                "endpoint_id": row["endpoint_id"],
                "endpoint_addrs": addresses,
                "peer_class": row["peer_class"],
                "runtime_capability": runtime_capability,
                "verification_key_digest": row["verification_key_digest"],
                "incarnation": row["incarnation"],
                "generation": generation,
                "lease_expires_at": float(row["lease_expires_at"]),
                "last_heartbeat_sequence": heartbeat_sequence,
                "last_liveness_at": float(row["last_liveness_at"]),
                "next_heartbeat_due_at": float(row["next_heartbeat_due_at"]),
                "last_activity_receipt_at": (
                    None
                    if activity_receipt is None
                    else float(activity_receipt)
                ),
                "active_requests": active_requests,
                "lifecycle_state": row["lifecycle_state"],
            }
        except SeedStateError:
            raise
        except (
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
        ) as exc:
            raise SeedStateError("seed_state_corrupt") from exc

    @contextmanager
    def member_authority_guard(
        self,
        *,
        node_id: str,
    ) -> Iterator[dict[str, Any]]:
        """Hold a write reservation around one strict persisted-member check."""

        if not isinstance(node_id, str) or not node_id:
            raise ValueError("node_id is invalid")
        connection: sqlite3.Connection | None = None
        transaction_open = False
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            transaction_open = True
            row = connection.execute(
                """
                SELECT node_id, endpoint_id, endpoint_addrs_json,
                       peer_class, runtime_capability_json,
                       verification_key_digest, incarnation, generation,
                       lease_expires_at, last_heartbeat_sequence,
                       last_liveness_at, next_heartbeat_due_at,
                       last_activity_receipt_at, active_requests, lifecycle_state
                FROM seed_members
                WHERE node_id = ?
                """,
                (node_id,),
            ).fetchone()
            if row is None:
                raise SeedStateError("seed_state_member_stale")
            member = self._decode_member_row(row)
            yield member
            connection.commit()
            transaction_open = False
        except SeedStateError:
            if connection is not None and transaction_open:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise
        except sqlite3.Error as exc:
            if connection is not None and transaction_open:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise SeedStateError(_sqlite_state_error_code(exc)) from exc
        except BaseException:
            if connection is not None and transaction_open:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    def load_assignments(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT assignment_id, node_id, deployment_id, deployment_epoch,
                       membership_generation, accepted, result_code,
                       load_proof_digest, runtime_endpoint
                FROM seed_assignments
                """
            ).fetchall()
            return [
                {
                    "assignment_id": row["assignment_id"],
                    "node_id": row["node_id"],
                    "deployment_id": row["deployment_id"],
                    "deployment_epoch": int(row["deployment_epoch"]),
                    "membership_generation": int(row["membership_generation"]),
                    "accepted": (
                        None if row["accepted"] is None else bool(row["accepted"])
                    ),
                    "result_code": row["result_code"],
                    "load_proof_digest": row["load_proof_digest"],
                    "runtime_endpoint": row["runtime_endpoint"],
                }
                for row in rows
            ]
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise SeedStateError("seed_state_corrupt") from exc
        finally:
            connection.close()

    @staticmethod
    def _decode_acceptance(raw_text: str) -> dict[str, Any]:
        try:
            raw = raw_text.encode("utf-8")
            value = json.loads(raw, parse_constant=_reject_json_constant)
            if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
                raise SeedStateError("seed_state_corrupt")
            return value
        except SeedStateError:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise SeedStateError("seed_state_corrupt") from exc

    @classmethod
    def _verify_heartbeat_envelope(
        cls,
        envelope: Mapping[str, Any],
        *,
        node_id: str,
        endpoint_id: str,
        verification_key_digest: str,
        incarnation: str,
        generation: int,
        heartbeat_message_id: str,
        heartbeat_sequence: int,
        swarm_id: str,
        seed_node_id: str,
    ) -> dict[str, Any]:
        try:
            untrusted_message = envelope["message"]
            issued_at = untrusted_message["issued_at"]
            if (
                isinstance(issued_at, bool)
                or not isinstance(issued_at, (int, float))
                or not math.isfinite(float(issued_at))
            ):
                raise SeedStateError("seed_state_corrupt")
            message = verify_membership_message(
                envelope,
                now=float(issued_at),
                expected_key_digest=verification_key_digest,
                expected_protocol=HEARTBEAT_PROTOCOL,
                expected_swarm_id=swarm_id,
                expected_sender_node_id=node_id,
                expected_sender_endpoint_id=endpoint_id,
                expected_recipient_node_id=seed_node_id,
            )
            if (
                type(message["generation"]) is not int
                or type(message["heartbeat_sequence"]) is not int
                or message["incarnation"] != incarnation
                or message["generation"] != generation
                or message["message_id"] != heartbeat_message_id
                or message["heartbeat_sequence"] != heartbeat_sequence
            ):
                raise SeedStateError("seed_state_corrupt")
            return message
        except SeedStateError:
            raise
        except (
            IndexError,
            KeyError,
            MembershipContractError,
            TypeError,
            ValueError,
        ) as exc:
            raise SeedStateError("seed_state_corrupt") from exc

    @classmethod
    def _verify_renewal_envelope(
        cls,
        envelope: Mapping[str, Any],
        *,
        node_id: str,
        incarnation: str,
        generation: int,
        heartbeat_message_id: str,
        renewal_message_id: str,
        swarm_id: str,
        seed_node_id: str,
        seed_key_digest: str,
    ) -> dict[str, Any]:
        try:
            untrusted_message = envelope["message"]
            issued_at = untrusted_message["issued_at"]
            if (
                isinstance(issued_at, bool)
                or not isinstance(issued_at, (int, float))
                or not math.isfinite(float(issued_at))
            ):
                raise SeedStateError("seed_state_corrupt")
            message = verify_membership_message(
                envelope,
                now=float(issued_at),
                expected_key_digest=seed_key_digest,
                expected_protocol=LEASE_RENEWAL_PROTOCOL,
                expected_swarm_id=swarm_id,
                expected_sender_node_id=seed_node_id,
                expected_recipient_node_id=node_id,
            )
            if (
                type(message["generation"]) is not int
                or type(message["membership_generation"]) is not int
                or message["message_id"] != renewal_message_id
                or message["heartbeat_message_id"] != heartbeat_message_id
                or message["generation"] != generation
                or message["membership_generation"] != generation
                or message["member_incarnation"] != incarnation
            ):
                raise SeedStateError("seed_state_corrupt")
            return message
        except SeedStateError:
            raise
        except (
            IndexError,
            KeyError,
            MembershipContractError,
            TypeError,
            ValueError,
        ) as exc:
            raise SeedStateError("seed_state_corrupt") from exc

    @classmethod
    def _decode_heartbeat_renewal_row(
        cls,
        row: sqlite3.Row,
        *,
        expected_node_id: str,
        expected_generation: int,
        expected_heartbeat_message_id: str,
        expected_endpoint_id: str | None = None,
        expected_verification_key_digest: str | None = None,
        expected_incarnation: str | None = None,
    ) -> tuple[str, int, dict[str, Any], dict[str, Any]]:
        """Decode and cross-check one durable heartbeat response binding."""

        try:
            text_fields = (
                "node_id",
                "heartbeat_message_id",
                "request_envelope_digest",
                "heartbeat_json",
                "renewal_message_id",
                "renewal_json",
                "current_endpoint_id",
                "current_verification_key_digest",
                "current_incarnation",
                "current_swarm_id",
                "current_seed_node_id",
                "current_seed_key_digest",
            )
            if any(
                not isinstance(row[field], str) or not row[field]
                for field in text_fields
            ):
                raise SeedStateError("seed_state_corrupt")
            generation = row["generation"]
            heartbeat_sequence = row["heartbeat_sequence"]
            current_generation = row["current_generation"]
            current_heartbeat_sequence = row["current_heartbeat_sequence"]
            if (
                type(generation) is not int
                or type(heartbeat_sequence) is not int
                or type(current_generation) is not int
                or type(current_heartbeat_sequence) is not int
                or generation != expected_generation
                or current_generation != expected_generation
                or heartbeat_sequence < 1
                or heartbeat_sequence > current_heartbeat_sequence
                or _SHA256_HEX_RE.fullmatch(
                    row["request_envelope_digest"]
                )
                is None
                or row["node_id"] != expected_node_id
                or row["heartbeat_message_id"]
                != expected_heartbeat_message_id
                or (
                    expected_endpoint_id is not None
                    and row["current_endpoint_id"] != expected_endpoint_id
                )
                or (
                    expected_verification_key_digest is not None
                    and row["current_verification_key_digest"]
                    != expected_verification_key_digest
                )
                or (
                    expected_incarnation is not None
                    and row["current_incarnation"] != expected_incarnation
                )
            ):
                raise SeedStateError("seed_state_corrupt")

            heartbeat = cls._decode_acceptance(row["heartbeat_json"])
            cls._verify_heartbeat_envelope(
                heartbeat,
                node_id=expected_node_id,
                endpoint_id=row["current_endpoint_id"],
                verification_key_digest=row[
                    "current_verification_key_digest"
                ],
                incarnation=row["current_incarnation"],
                generation=expected_generation,
                heartbeat_message_id=expected_heartbeat_message_id,
                heartbeat_sequence=heartbeat_sequence,
                swarm_id=row["current_swarm_id"],
                seed_node_id=row["current_seed_node_id"],
            )
            heartbeat_raw = row["heartbeat_json"].encode("utf-8")
            if (
                hashlib.sha256(heartbeat_raw).hexdigest()
                != row["request_envelope_digest"]
            ):
                raise SeedStateError("seed_state_corrupt")

            renewal = cls._decode_acceptance(row["renewal_json"])
            cls._verify_renewal_envelope(
                renewal,
                node_id=expected_node_id,
                incarnation=row["current_incarnation"],
                generation=expected_generation,
                heartbeat_message_id=expected_heartbeat_message_id,
                renewal_message_id=row["renewal_message_id"],
                swarm_id=row["current_swarm_id"],
                seed_node_id=row["current_seed_node_id"],
                seed_key_digest=row["current_seed_key_digest"],
            )
            return (
                row["request_envelope_digest"],
                heartbeat_sequence,
                heartbeat,
                renewal,
            )
        except SeedStateError:
            raise
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise SeedStateError("seed_state_corrupt") from exc

    def load_join_acceptance(
        self,
        *,
        nonce: str,
        invite_token_digest: str,
        request_envelope_digest: str,
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT invite_token_digest, request_envelope_digest,
                       acceptance_json
                FROM seed_join_acceptances WHERE nonce = ?
                """,
                (nonce,),
            ).fetchone()
            if row is None:
                return None
            if (
                row["invite_token_digest"] != invite_token_digest
                or row["request_envelope_digest"] != request_envelope_digest
            ):
                raise SeedStateError("seed_join_retry_mismatch")
            return self._decode_acceptance(row["acceptance_json"])
        except SeedStateError:
            raise
        except sqlite3.Error as exc:
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def load_heartbeat_renewal(
        self,
        *,
        node_id: str,
        endpoint_id: str,
        verification_key_digest: str,
        incarnation: str,
        generation: int,
        heartbeat_message_id: str,
        heartbeat_sequence: int,
        request_envelope_digest: str,
    ) -> dict[str, Any] | None:
        """Return an exact committed renewal for the current bound member."""

        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT renewal.node_id, renewal.generation,
                       renewal.heartbeat_message_id,
                       renewal.request_envelope_digest,
                       renewal.heartbeat_sequence,
                       renewal.heartbeat_json,
                       renewal.renewal_message_id, renewal.renewal_json,
                       member.endpoint_id AS current_endpoint_id,
                       member.verification_key_digest
                           AS current_verification_key_digest,
                       member.incarnation AS current_incarnation,
                       member.generation AS current_generation,
                       member.last_heartbeat_sequence
                           AS current_heartbeat_sequence,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'swarm_id') AS current_swarm_id,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'seed_node_id') AS current_seed_node_id,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'seed_key_digest') AS current_seed_key_digest
                FROM seed_heartbeat_renewals AS renewal
                JOIN seed_members AS member
                  ON member.node_id = renewal.node_id
                 AND member.generation = renewal.generation
                WHERE renewal.node_id = ? AND renewal.generation = ?
                  AND renewal.heartbeat_message_id = ?
                  AND member.endpoint_id = ?
                  AND member.verification_key_digest = ?
                  AND member.incarnation = ?
                """,
                (
                    node_id,
                    generation,
                    heartbeat_message_id,
                    endpoint_id,
                    verification_key_digest,
                    incarnation,
                ),
            ).fetchone()
            if row is None:
                return None
            (
                stored_request_digest,
                stored_heartbeat_sequence,
                _stored_heartbeat,
                renewal,
            ) = self._decode_heartbeat_renewal_row(
                row,
                expected_node_id=node_id,
                expected_generation=generation,
                expected_heartbeat_message_id=heartbeat_message_id,
                expected_endpoint_id=endpoint_id,
                expected_verification_key_digest=verification_key_digest,
                expected_incarnation=incarnation,
            )
            if stored_request_digest != request_envelope_digest:
                raise SeedStateError("seed_heartbeat_retry_mismatch")
            if stored_heartbeat_sequence != heartbeat_sequence:
                raise SeedStateError("seed_heartbeat_retry_mismatch")
            return renewal
        except SeedStateError:
            raise
        except sqlite3.Error as exc:
            raise SeedStateError(_sqlite_state_error_code(exc)) from exc
        finally:
            connection.close()

    def find_heartbeat_renewal(
        self,
        *,
        node_id: str,
        generation: int,
        heartbeat_message_id: str,
    ) -> dict[str, Any] | None:
        """Recover a committed renewal only for the current member generation."""

        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT renewal.node_id, renewal.generation,
                       renewal.heartbeat_message_id,
                       renewal.request_envelope_digest,
                       renewal.heartbeat_sequence,
                       renewal.heartbeat_json,
                       renewal.renewal_message_id, renewal.renewal_json,
                       member.endpoint_id AS current_endpoint_id,
                       member.verification_key_digest
                           AS current_verification_key_digest,
                       member.incarnation AS current_incarnation,
                       member.generation AS current_generation,
                       member.last_heartbeat_sequence
                           AS current_heartbeat_sequence,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'swarm_id') AS current_swarm_id,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'seed_node_id') AS current_seed_node_id,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'seed_key_digest') AS current_seed_key_digest
                FROM seed_heartbeat_renewals AS renewal
                JOIN seed_members AS member
                  ON member.node_id = renewal.node_id
                 AND member.generation = renewal.generation
                WHERE renewal.node_id = ? AND renewal.generation = ?
                  AND renewal.heartbeat_message_id = ?
                """,
                (node_id, generation, heartbeat_message_id),
            ).fetchone()
            if row is None:
                return None
            _request_digest, _heartbeat_sequence, _heartbeat, renewal = (
                self._decode_heartbeat_renewal_row(
                    row,
                    expected_node_id=node_id,
                    expected_generation=generation,
                    expected_heartbeat_message_id=heartbeat_message_id,
                )
            )
            return renewal
        except SeedStateError:
            raise
        except sqlite3.Error as exc:
            raise SeedStateError(_sqlite_state_error_code(exc)) from exc
        finally:
            connection.close()

    def commit_heartbeat_renewal(
        self,
        *,
        request_envelope_digest: str,
        heartbeat: Mapping[str, Any],
        heartbeat_message_id: str,
        heartbeat_sequence: int,
        heartbeat_expires_at: float,
        renewal_message_id: str,
        member: Mapping[str, Any],
        renewal: Mapping[str, Any],
        now: float,
        capacity: int,
    ) -> dict[str, Any]:
        """Atomically accept a heartbeat and persist its exact signed response."""

        try:
            heartbeat_json = canonical_json_bytes(dict(heartbeat)).decode("utf-8")
            renewal_json = canonical_json_bytes(dict(renewal)).decode("utf-8")
            node_id = member["node_id"]
            generation = member["generation"]
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise SeedStateError("seed_state_corrupt") from exc
        if (
            not isinstance(node_id, str)
            or not node_id
            or type(generation) is not int
            or generation < 1
            or type(heartbeat_sequence) is not int
            or heartbeat_sequence < 1
            or not isinstance(request_envelope_digest, str)
            or _SHA256_HEX_RE.fullmatch(request_envelope_digest) is None
            or hashlib.sha256(heartbeat_json.encode("utf-8")).hexdigest()
            != request_envelope_digest
        ):
            raise SeedStateError("seed_state_corrupt")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT endpoint_id, verification_key_digest, incarnation,
                       generation, last_heartbeat_sequence,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'swarm_id') AS current_swarm_id,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'seed_node_id') AS current_seed_node_id,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'seed_key_digest') AS current_seed_key_digest
                FROM seed_members WHERE node_id = ?
                """,
                (node_id,),
            ).fetchone()
            if current is None:
                raise SeedStateError("seed_state_member_stale")
            if (
                type(current["generation"]) is not int
                or type(current["last_heartbeat_sequence"]) is not int
            ):
                raise SeedStateError("seed_state_corrupt")
            if (
                current["endpoint_id"] != member["endpoint_id"]
                or current["verification_key_digest"]
                != member["verification_key_digest"]
                or current["incarnation"] != member["incarnation"]
                or current["generation"] != generation
            ):
                raise SeedStateError("seed_state_member_stale")

            existing = connection.execute(
                """
                SELECT renewal.node_id, renewal.generation,
                       renewal.heartbeat_message_id,
                       renewal.request_envelope_digest,
                       renewal.heartbeat_sequence,
                       renewal.heartbeat_json,
                       renewal.renewal_message_id, renewal.renewal_json,
                       member.endpoint_id AS current_endpoint_id,
                       member.verification_key_digest
                           AS current_verification_key_digest,
                       member.incarnation AS current_incarnation,
                       member.generation AS current_generation,
                       member.last_heartbeat_sequence
                           AS current_heartbeat_sequence,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'swarm_id') AS current_swarm_id,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'seed_node_id') AS current_seed_node_id,
                       (SELECT value FROM seed_metadata
                        WHERE key = 'seed_key_digest') AS current_seed_key_digest
                FROM seed_heartbeat_renewals AS renewal
                JOIN seed_members AS member
                  ON member.node_id = renewal.node_id
                 AND member.generation = renewal.generation
                WHERE renewal.node_id = ? AND renewal.generation = ?
                  AND renewal.heartbeat_message_id = ?
                """,
                (node_id, generation, heartbeat_message_id),
            ).fetchone()
            if existing is not None:
                (
                    stored_request_digest,
                    stored_heartbeat_sequence,
                    stored_heartbeat,
                    stored_renewal,
                ) = self._decode_heartbeat_renewal_row(
                    existing,
                    expected_node_id=node_id,
                    expected_generation=generation,
                    expected_heartbeat_message_id=heartbeat_message_id,
                    expected_endpoint_id=member["endpoint_id"],
                    expected_verification_key_digest=member[
                        "verification_key_digest"
                    ],
                    expected_incarnation=member["incarnation"],
                )
                if stored_request_digest != request_envelope_digest:
                    raise SeedStateError("seed_heartbeat_retry_mismatch")
                if stored_heartbeat_sequence != heartbeat_sequence:
                    raise SeedStateError("seed_heartbeat_retry_mismatch")
                if canonical_json_bytes(stored_heartbeat) != heartbeat_json.encode(
                    "utf-8"
                ):
                    raise SeedStateError("seed_heartbeat_retry_mismatch")
                connection.commit()
                return stored_renewal
            if heartbeat_sequence <= current["last_heartbeat_sequence"]:
                raise SeedStateError("seed_state_member_conflict")

            text_metadata = (
                current["current_swarm_id"],
                current["current_seed_node_id"],
                current["current_seed_key_digest"],
            )
            if any(not isinstance(value, str) or not value for value in text_metadata):
                raise SeedStateError("seed_state_corrupt")
            heartbeat_message = self._verify_heartbeat_envelope(
                heartbeat,
                node_id=node_id,
                endpoint_id=current["endpoint_id"],
                verification_key_digest=current["verification_key_digest"],
                incarnation=current["incarnation"],
                generation=generation,
                heartbeat_message_id=heartbeat_message_id,
                heartbeat_sequence=heartbeat_sequence,
                swarm_id=current["current_swarm_id"],
                seed_node_id=current["current_seed_node_id"],
            )
            if (
                isinstance(heartbeat_expires_at, bool)
                or not isinstance(heartbeat_expires_at, (int, float))
                or not math.isfinite(float(heartbeat_expires_at))
                or float(heartbeat_message["expires_at"])
                != float(heartbeat_expires_at)
                or type(member["last_heartbeat_sequence"]) is not int
                or member["last_heartbeat_sequence"] != heartbeat_sequence
            ):
                raise SeedStateError("seed_state_corrupt")
            self._verify_renewal_envelope(
                renewal,
                node_id=node_id,
                incarnation=current["incarnation"],
                generation=generation,
                heartbeat_message_id=heartbeat_message_id,
                renewal_message_id=renewal_message_id,
                swarm_id=current["current_swarm_id"],
                seed_node_id=current["current_seed_node_id"],
                seed_key_digest=current["current_seed_key_digest"],
            )

            connection.execute(
                "DELETE FROM seed_replay WHERE expires_at < ?",
                (now,),
            )
            if connection.execute(
                """
                SELECT 1 FROM seed_replay
                WHERE node_id = ? AND generation = ? AND message_id = ?
                """,
                (node_id, generation, heartbeat_message_id),
            ).fetchone() is not None:
                raise SeedStateError("seed_message_replayed")
            count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM seed_replay
                WHERE node_id = ? AND generation = ?
                """,
                (node_id, generation),
            ).fetchone()["count"]
            if int(count) >= capacity:
                raise SeedStateError("seed_replay_window_full")
            if connection.execute(
                "SELECT 1 FROM seed_emitted_messages WHERE message_id = ?",
                (renewal_message_id,),
            ).fetchone() is not None:
                raise SeedStateError("seed_message_id_reused")

            connection.execute(
                """
                INSERT INTO seed_replay (
                    node_id, generation, message_id, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    node_id,
                    generation,
                    heartbeat_message_id,
                    heartbeat_expires_at,
                ),
            )
            connection.execute(
                "INSERT INTO seed_emitted_messages (message_id) VALUES (?)",
                (renewal_message_id,),
            )
            cursor = connection.execute(
                """
                UPDATE seed_members SET
                    lease_expires_at = ?,
                    last_heartbeat_sequence = ?,
                    last_liveness_at = ?,
                    next_heartbeat_due_at = ?,
                    last_activity_receipt_at = ?,
                    active_requests = ?,
                    lifecycle_state = ?
                WHERE node_id = ? AND endpoint_id = ?
                  AND verification_key_digest = ? AND incarnation = ?
                  AND generation = ? AND last_heartbeat_sequence < ?
                """,
                (
                    member["lease_expires_at"],
                    heartbeat_sequence,
                    member["last_liveness_at"],
                    member["next_heartbeat_due_at"],
                    member["last_activity_receipt_at"],
                    member["active_requests"],
                    member["lifecycle_state"],
                    node_id,
                    member["endpoint_id"],
                    member["verification_key_digest"],
                    member["incarnation"],
                    generation,
                    heartbeat_sequence,
                ),
            )
            if cursor.rowcount != 1:
                raise SeedStateError("seed_state_member_conflict")
            connection.execute(
                """
                INSERT INTO seed_heartbeat_renewals (
                    node_id, generation, heartbeat_message_id,
                    request_envelope_digest, heartbeat_sequence,
                    heartbeat_json, renewal_message_id, renewal_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    generation,
                    heartbeat_message_id,
                    request_envelope_digest,
                    heartbeat_sequence,
                    heartbeat_json,
                    renewal_message_id,
                    renewal_json,
                ),
            )
            connection.commit()
            return dict(renewal)
        except SeedStateError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError(_sqlite_state_error_code(exc)) from exc
        finally:
            connection.close()

    def commit_join(
        self,
        *,
        nonce: str,
        consumed_at: float,
        invite_expires_at: float,
        invite_token_digest: str,
        request_envelope_digest: str,
        request_message_id: str,
        message_id: str,
        member: Mapping[str, Any],
        acceptance: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically consume invite, persist member, and store exact acceptance."""

        addresses = canonical_json_bytes(list(member["endpoint_addrs"])).decode("utf-8")
        runtime_capability = canonical_json_bytes(
            dict(member["runtime_capability"])
        ).decode("utf-8")
        acceptance_json = canonical_json_bytes(dict(acceptance)).decode("utf-8")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT invite_token_digest, request_envelope_digest,
                       acceptance_json
                FROM seed_join_acceptances WHERE nonce = ?
                """,
                (nonce,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["invite_token_digest"] != invite_token_digest
                    or existing["request_envelope_digest"] != request_envelope_digest
                ):
                    raise SeedStateError("seed_join_retry_mismatch")
                connection.commit()
                return self._decode_acceptance(existing["acceptance_json"])
            if connection.execute(
                "SELECT 1 FROM consumed_invites WHERE nonce = ?", (nonce,)
            ).fetchone() is not None:
                raise SeedStateError("seed_join_invite_replayed")
            if connection.execute(
                "SELECT 1 FROM seed_emitted_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone() is not None:
                raise SeedStateError("seed_message_id_reused")

            connection.execute(
                """
                INSERT INTO consumed_invites (nonce, consumed_at, expires_at)
                VALUES (?, ?, ?)
                """,
                (nonce, consumed_at, invite_expires_at),
            )
            cursor = connection.execute(
                """
                INSERT INTO seed_members (
                    node_id, endpoint_id, endpoint_addrs_json,
                    peer_class, runtime_capability_json,
                    verification_key_digest, incarnation, generation,
                    lease_expires_at, last_heartbeat_sequence,
                    last_liveness_at, next_heartbeat_due_at,
                    last_activity_receipt_at, active_requests, lifecycle_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    endpoint_id = excluded.endpoint_id,
                    endpoint_addrs_json = excluded.endpoint_addrs_json,
                    peer_class = excluded.peer_class,
                    runtime_capability_json = excluded.runtime_capability_json,
                    verification_key_digest = excluded.verification_key_digest,
                    incarnation = excluded.incarnation,
                    generation = excluded.generation,
                    lease_expires_at = excluded.lease_expires_at,
                    last_heartbeat_sequence = excluded.last_heartbeat_sequence,
                    last_liveness_at = excluded.last_liveness_at,
                    next_heartbeat_due_at = excluded.next_heartbeat_due_at,
                    last_activity_receipt_at = excluded.last_activity_receipt_at,
                    active_requests = excluded.active_requests,
                    lifecycle_state = excluded.lifecycle_state
                WHERE
                    seed_members.verification_key_digest =
                        excluded.verification_key_digest
                    AND seed_members.endpoint_id = excluded.endpoint_id
                    AND excluded.generation > seed_members.generation
                """,
                (
                    member["node_id"],
                    member["endpoint_id"],
                    addresses,
                    member["peer_class"],
                    runtime_capability,
                    member["verification_key_digest"],
                    member["incarnation"],
                    member["generation"],
                    member["lease_expires_at"],
                    member["last_heartbeat_sequence"],
                    member["last_liveness_at"],
                    member["next_heartbeat_due_at"],
                    member["last_activity_receipt_at"],
                    member["active_requests"],
                    member["lifecycle_state"],
                ),
            )
            if cursor.rowcount != 1:
                raise SeedStateError("seed_state_member_conflict")
            connection.execute(
                "INSERT INTO seed_emitted_messages (message_id) VALUES (?)",
                (message_id,),
            )
            connection.execute(
                """
                INSERT INTO seed_join_acceptances (
                    nonce, invite_token_digest, request_envelope_digest,
                    request_message_id, node_id, endpoint_id,
                    verification_key_digest, incarnation, generation,
                    acceptance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    nonce,
                    invite_token_digest,
                    request_envelope_digest,
                    request_message_id,
                    member["node_id"],
                    member["endpoint_id"],
                    member["verification_key_digest"],
                    member["incarnation"],
                    member["generation"],
                    acceptance_json,
                ),
            )
            connection.commit()
            return dict(acceptance)
        except SeedStateError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise SeedStateError("seed_join_conflict") from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def load_resume_acceptance(
        self,
        *,
        request_message_id: str,
        request_envelope_digest: str,
    ) -> dict[str, Any] | None:
        """Return one exact committed resume response for HTTP retry."""

        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT request_envelope_digest, acceptance_json
                FROM seed_resume_acceptances WHERE request_message_id = ?
                """,
                (request_message_id,),
            ).fetchone()
            if row is None:
                return None
            if row["request_envelope_digest"] != request_envelope_digest:
                raise SeedStateError("seed_resume_retry_mismatch")
            return self._decode_acceptance(row["acceptance_json"])
        except SeedStateError:
            raise
        except sqlite3.Error as exc:
            raise SeedStateError(_sqlite_state_error_code(exc)) from exc
        finally:
            connection.close()

    def commit_resume(
        self,
        *,
        request_message_id: str,
        request_envelope_digest: str,
        previous_incarnation: str,
        previous_generation: int,
        message_id: str,
        member: Mapping[str, Any],
        acceptance: Mapping[str, Any],
        already_advanced: bool = False,
    ) -> dict[str, Any]:
        """Atomically advance one durable member and store its resume response."""

        addresses = canonical_json_bytes(list(member["endpoint_addrs"])).decode("utf-8")
        runtime_capability = canonical_json_bytes(
            dict(member["runtime_capability"])
        ).decode("utf-8")
        acceptance_json = canonical_json_bytes(dict(acceptance)).decode("utf-8")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT request_envelope_digest, acceptance_json
                FROM seed_resume_acceptances WHERE request_message_id = ?
                """,
                (request_message_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_envelope_digest"] != request_envelope_digest:
                    raise SeedStateError("seed_resume_retry_mismatch")
                connection.commit()
                return self._decode_acceptance(existing["acceptance_json"])
            if connection.execute(
                "SELECT 1 FROM seed_emitted_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone() is not None:
                raise SeedStateError("seed_message_id_reused")

            if already_advanced:
                cursor = connection.execute(
                    """
                    UPDATE seed_members SET
                        endpoint_addrs_json = ?, lease_expires_at = ?,
                        last_heartbeat_sequence = ?, last_liveness_at = ?,
                        next_heartbeat_due_at = ?, last_activity_receipt_at = ?,
                        active_requests = ?, lifecycle_state = ?
                    WHERE node_id = ? AND endpoint_id = ?
                      AND verification_key_digest = ?
                      AND incarnation = ? AND generation = ?
                      AND lifecycle_state NOT IN ('STOPPING', 'STOPPED')
                    """,
                    (
                        addresses,
                        member["lease_expires_at"],
                        member["last_heartbeat_sequence"],
                        member["last_liveness_at"],
                        member["next_heartbeat_due_at"],
                        member["last_activity_receipt_at"],
                        member["active_requests"],
                        member["lifecycle_state"],
                        member["node_id"],
                        member["endpoint_id"],
                        member["verification_key_digest"],
                        member["incarnation"],
                        member["generation"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise SeedStateError("seed_state_member_conflict")
            else:
                cursor = connection.execute(
                    """
                    UPDATE seed_members SET
                        endpoint_addrs_json = ?, peer_class = ?,
                        runtime_capability_json = ?, incarnation = ?, generation = ?,
                        lease_expires_at = ?, last_heartbeat_sequence = ?,
                        last_liveness_at = ?, next_heartbeat_due_at = ?,
                        last_activity_receipt_at = ?, active_requests = ?,
                        lifecycle_state = ?
                    WHERE node_id = ? AND endpoint_id = ?
                      AND verification_key_digest = ?
                      AND incarnation = ? AND generation = ?
                      AND lifecycle_state NOT IN ('STOPPING', 'STOPPED')
                    """,
                    (
                        addresses,
                        member["peer_class"],
                        runtime_capability,
                        member["incarnation"],
                        member["generation"],
                        member["lease_expires_at"],
                        member["last_heartbeat_sequence"],
                        member["last_liveness_at"],
                        member["next_heartbeat_due_at"],
                        member["last_activity_receipt_at"],
                        member["active_requests"],
                        member["lifecycle_state"],
                        member["node_id"],
                        member["endpoint_id"],
                        member["verification_key_digest"],
                        previous_incarnation,
                        previous_generation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SeedStateError("seed_state_member_conflict")

            connection.execute(
                "INSERT INTO seed_emitted_messages (message_id) VALUES (?)",
                (message_id,),
            )
            connection.execute(
                """
                INSERT INTO seed_resume_acceptances (
                    request_message_id, request_envelope_digest, node_id,
                    previous_incarnation, accepted_incarnation,
                    previous_generation, generation, acceptance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_message_id,
                    request_envelope_digest,
                    member["node_id"],
                    previous_incarnation,
                    member["incarnation"],
                    previous_generation,
                    member["generation"],
                    acceptance_json,
                ),
            )
            connection.commit()
            return dict(acceptance)
        except SeedStateError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise SeedStateError("seed_resume_conflict") from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError(_sqlite_state_error_code(exc)) from exc
        finally:
            connection.close()

    def save_member(self, member: Mapping[str, Any]) -> None:
        addresses = canonical_json_bytes(list(member["endpoint_addrs"])).decode("utf-8")
        runtime_capability = canonical_json_bytes(
            dict(member["runtime_capability"])
        ).decode("utf-8")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO seed_members (
                    node_id, endpoint_id, endpoint_addrs_json,
                    peer_class, runtime_capability_json,
                    verification_key_digest, incarnation, generation,
                    lease_expires_at, last_heartbeat_sequence,
                    last_liveness_at, next_heartbeat_due_at,
                    last_activity_receipt_at, active_requests, lifecycle_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    endpoint_id = excluded.endpoint_id,
                    endpoint_addrs_json = excluded.endpoint_addrs_json,
                    peer_class = excluded.peer_class,
                    runtime_capability_json = excluded.runtime_capability_json,
                    verification_key_digest = excluded.verification_key_digest,
                    incarnation = excluded.incarnation,
                    generation = excluded.generation,
                    lease_expires_at = excluded.lease_expires_at,
                    last_heartbeat_sequence = excluded.last_heartbeat_sequence,
                    last_liveness_at = excluded.last_liveness_at,
                    next_heartbeat_due_at = excluded.next_heartbeat_due_at,
                    last_activity_receipt_at = excluded.last_activity_receipt_at,
                    active_requests = excluded.active_requests,
                    lifecycle_state = excluded.lifecycle_state
                WHERE
                    seed_members.verification_key_digest =
                        excluded.verification_key_digest
                    AND seed_members.endpoint_id = excluded.endpoint_id
                    AND (
                        excluded.generation > seed_members.generation
                        OR (
                            excluded.generation = seed_members.generation
                            AND excluded.incarnation = seed_members.incarnation
                            AND excluded.last_heartbeat_sequence >=
                                seed_members.last_heartbeat_sequence
                        )
                    )
                """,
                (
                    member["node_id"],
                    member["endpoint_id"],
                    addresses,
                    member["peer_class"],
                    runtime_capability,
                    member["verification_key_digest"],
                    member["incarnation"],
                    member["generation"],
                    member["lease_expires_at"],
                    member["last_heartbeat_sequence"],
                    member["last_liveness_at"],
                    member["next_heartbeat_due_at"],
                    member["last_activity_receipt_at"],
                    member["active_requests"],
                    member["lifecycle_state"],
                ),
            )
            if cursor.rowcount != 1:
                raise SeedStateError("seed_state_member_conflict")
            connection.commit()
        except SeedStateError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def reserve_seed_message(self, message_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO seed_emitted_messages (message_id) VALUES (?)",
                (message_id,),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise SeedStateError("seed_message_id_reused") from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def member_is_current(
        self,
        *,
        node_id: str,
        endpoint_id: str,
        verification_key_digest: str,
        incarnation: str,
        generation: int,
    ) -> bool:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT 1 FROM seed_members
                WHERE node_id = ? AND endpoint_id = ?
                  AND verification_key_digest = ? AND incarnation = ?
                  AND generation = ?
                """,
                (
                    node_id,
                    endpoint_id,
                    verification_key_digest,
                    incarnation,
                    generation,
                ),
            ).fetchone()
            return row is not None
        except sqlite3.Error as exc:
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def member_message_seen(
        self,
        *,
        node_id: str,
        generation: int,
        message_id: str,
    ) -> bool:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT 1 FROM seed_replay
                WHERE node_id = ? AND generation = ? AND message_id = ?
                """,
                (node_id, generation, message_id),
            ).fetchone()
            return row is not None
        except sqlite3.Error as exc:
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def remember_member_message(
        self,
        *,
        node_id: str,
        generation: int,
        message_id: str,
        expires_at: float,
        now: float,
        capacity: int,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM seed_replay WHERE expires_at < ?",
                (now,),
            )
            count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM seed_replay
                WHERE node_id = ? AND generation = ?
                """,
                (node_id, generation),
            ).fetchone()["count"]
            if int(count) >= capacity:
                raise SeedStateError("seed_replay_window_full")
            connection.execute(
                """
                INSERT INTO seed_replay (
                    node_id, generation, message_id, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (node_id, generation, message_id, expires_at),
            )
            connection.commit()
        except SeedStateError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise SeedStateError("seed_message_replayed") from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def save_assignment(self, assignment: Mapping[str, Any]) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT generation FROM seed_members WHERE node_id = ?",
                (assignment["node_id"],),
            ).fetchone()
            if (
                current is None
                or int(current["generation"])
                != int(assignment["membership_generation"])
            ):
                raise SeedStateError("seed_state_member_stale")
            connection.execute(
                """
                INSERT INTO seed_assignments (
                    assignment_id, node_id, deployment_id, deployment_epoch,
                    membership_generation, accepted, result_code,
                    load_proof_digest, runtime_endpoint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment["assignment_id"],
                    assignment["node_id"],
                    assignment["deployment_id"],
                    assignment["deployment_epoch"],
                    assignment["membership_generation"],
                    assignment["accepted"],
                    assignment["result_code"],
                    assignment["load_proof_digest"],
                    assignment["runtime_endpoint"],
                ),
            )
            connection.commit()
        except SeedStateError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise SeedStateError("seed_assignment_exists") from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

    def save_assignment_result(self, assignment: Mapping[str, Any]) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE seed_assignments SET
                    accepted = ?, result_code = ?, load_proof_digest = ?,
                    runtime_endpoint = ?
                WHERE assignment_id = ? AND accepted IS NULL
                  AND membership_generation = (
                      SELECT generation FROM seed_members
                      WHERE node_id = seed_assignments.node_id
                  )
                """,
                (
                    assignment["accepted"],
                    assignment["result_code"],
                    assignment["load_proof_digest"],
                    assignment["runtime_endpoint"],
                    assignment["assignment_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise SeedStateError("seed_assignment_result_already_recorded")
            connection.commit()
        except SeedStateError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()
