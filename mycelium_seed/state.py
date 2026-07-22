# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable SQLite state for the seed coordinator."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import Any

from mycelium_invite import SqliteInviteRegistry
from mycelium_qualification.evidence import canonical_json_bytes


_SCHEMA_VERSION = 3


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
                        CHECK (last_heartbeat_sequence >= 0)
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
            elif row["value"] in {"1", "2"}:
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
                connection.execute(
                    "UPDATE seed_metadata SET value = ? WHERE key = 'schema_version'",
                    (str(_SCHEMA_VERSION),),
                )
            elif row["value"] != str(_SCHEMA_VERSION):
                raise SeedStateError("seed_state_schema_unsupported")
            connection.commit()
        except SeedStateError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise SeedStateError("seed_state_unavailable") from exc
        finally:
            connection.close()

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
                       lease_expires_at, last_heartbeat_sequence
                FROM seed_members
                """
            ).fetchall()
            members: list[dict[str, Any]] = []
            for row in rows:
                raw = row["endpoint_addrs_json"].encode("utf-8")
                addresses = json.loads(raw)
                runtime_raw = row["runtime_capability_json"].encode("utf-8")
                runtime_capability = json.loads(runtime_raw)
                if (
                    not isinstance(addresses, list)
                    or not addresses
                    or not all(isinstance(value, str) and value for value in addresses)
                    or canonical_json_bytes(addresses) != raw
                    or not isinstance(runtime_capability, dict)
                    or canonical_json_bytes(runtime_capability) != runtime_raw
                ):
                    raise SeedStateError("seed_state_corrupt")
                members.append(
                    {
                        "node_id": row["node_id"],
                        "endpoint_id": row["endpoint_id"],
                        "endpoint_addrs": addresses,
                        "peer_class": row["peer_class"],
                        "runtime_capability": runtime_capability,
                        "verification_key_digest": row["verification_key_digest"],
                        "incarnation": row["incarnation"],
                        "generation": int(row["generation"]),
                        "lease_expires_at": float(row["lease_expires_at"]),
                        "last_heartbeat_sequence": int(
                            row["last_heartbeat_sequence"]
                        ),
                    }
                )
            return members
        except SeedStateError:
            raise
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SeedStateError("seed_state_corrupt") from exc
        finally:
            connection.close()

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
            value = json.loads(raw)
            if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
                raise SeedStateError("seed_state_corrupt")
            return value
        except SeedStateError:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
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
                    lease_expires_at, last_heartbeat_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    endpoint_id = excluded.endpoint_id,
                    endpoint_addrs_json = excluded.endpoint_addrs_json,
                    peer_class = excluded.peer_class,
                    runtime_capability_json = excluded.runtime_capability_json,
                    verification_key_digest = excluded.verification_key_digest,
                    incarnation = excluded.incarnation,
                    generation = excluded.generation,
                    lease_expires_at = excluded.lease_expires_at,
                    last_heartbeat_sequence = excluded.last_heartbeat_sequence
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
                    lease_expires_at, last_heartbeat_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    endpoint_id = excluded.endpoint_id,
                    endpoint_addrs_json = excluded.endpoint_addrs_json,
                    peer_class = excluded.peer_class,
                    runtime_capability_json = excluded.runtime_capability_json,
                    verification_key_digest = excluded.verification_key_digest,
                    incarnation = excluded.incarnation,
                    generation = excluded.generation,
                    lease_expires_at = excluded.lease_expires_at,
                    last_heartbeat_sequence = excluded.last_heartbeat_sequence
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
