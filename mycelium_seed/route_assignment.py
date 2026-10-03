# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner-issued, seed-signed route assignments for enrolled native members.

The minimal A2 slice the release node app needs before an artifact client and
planner placement exist: the owner (with the seed state root) records ONE route
assignment for one enrolled member -- a digest-bound stage pack, the route peer's
iroh endpoint id and the route generation.  The running seed hands it out only to
that member, over the existing member channel (``POST /seed/message``), in
answer to a request the member signs with its enrolled key and current
membership generation.  The answer is a seed-signed statement bound to the
member's node id, endpoint id, current membership generation and the request's
message id, so a node accepts a peer only on its pinned seed's word and never
from a file, intent or deep link.  The stage pack itself travels next to the
signed statement and is bound to it by its content digest, which the node
recomputes before loading.

Assignments live in their own table of the seed database so the owner's
operator command and the running seed share them without restarting the seed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from mycelium_membership import peer_runtime_is_activation_eligible
from mycelium_mobile.pixel_stage import PixelStage, PixelStageError
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import build_ed25519_verifier

from .coordinator import SeedCoordinatorError
from .operator import SeedOperatorError, _load_authority


ROUTE_ASSIGNMENT_REQUEST_PROTOCOL = "mycelium.seed.route_assignment_request.v1"
ROUTE_ASSIGNMENT_PROTOCOL = "mycelium.seed.route_assignment.v1"
ROUTE_ASSIGNMENT_DELIVERY_PROTOCOL = "mycelium.seed.route_assignment_delivery.v1"
ROUTE_ASSIGNMENT_RECORD_PROTOCOL = "mycelium.seed.route_assignment_record.v1"
SIGNED_MESSAGE_PROTOCOL = "mycelium.membership.signed_message.v1"

MAX_REQUEST_TTL_SECONDS = 120.0
MAX_CLOCK_SKEW_SECONDS = 30.0
DELIVERY_TTL_SECONDS = 60.0
DEFAULT_ASSIGNMENT_TTL_SECONDS = 6 * 3_600.0
MAX_ASSIGNMENT_TTL_SECONDS = 24 * 3_600.0
MAX_STAGE_PACK_BYTES = 512 * 1024
#: Same per-member-generation replay window the coordinator keeps.
MAX_REPLAY_IDS = 4096
_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "message_id",
        "swarm_id",
        "sender_node_id",
        "sender_endpoint_id",
        "recipient_node_id",
        "incarnation",
        "generation",
        "issued_at",
        "expires_at",
        "applied_assignment_id",
    }
)
_ENVELOPE_FIELDS = frozenset({"protocol", "message", "signature", "verification_key"})
_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ENDPOINT_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ANDROID_PEER_CLASS = "android_native_iroh"


def _fail(code: str) -> SeedCoordinatorError:
    return SeedCoordinatorError(code)


def _segment(value: Any) -> bool:
    return isinstance(value, str) and _SEGMENT_RE.fullmatch(value) is not None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


_SCHEMA = """
CREATE TABLE IF NOT EXISTS seed_route_assignments (
    node_id TEXT PRIMARY KEY NOT NULL,
    assignment_id TEXT NOT NULL UNIQUE,
    endpoint_id TEXT NOT NULL,
    verification_key_digest TEXT NOT NULL,
    stage_pack_json TEXT NOT NULL,
    stage_pack_digest TEXT NOT NULL,
    peer_endpoint_id TEXT NOT NULL,
    route_generation INTEGER NOT NULL CHECK (route_generation >= 1),
    forward_to INTEGER NOT NULL CHECK (forward_to IN (0, 1)),
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    withdrawn INTEGER NOT NULL DEFAULT 0 CHECK (withdrawn IN (0, 1)),
    delivered_at REAL,
    delivered_generation INTEGER,
    applied_at REAL,
    applied_generation INTEGER
) WITHOUT ROWID
"""


def _ensure_schema(database: Path) -> None:
    connection = _connect(database)
    try:
        connection.execute(_SCHEMA)
    except sqlite3.Error as exc:
        raise _fail("seed_route_assignment_store_unavailable") from exc
    finally:
        connection.close()


def _member_row(connection: sqlite3.Connection, node_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT node_id, endpoint_id, peer_class, runtime_capability_json,
               verification_key_digest, incarnation, generation,
               lease_expires_at, lifecycle_state
        FROM seed_members WHERE node_id = ?
        """,
        (node_id,),
    ).fetchone()


def _member_eligible(row: sqlite3.Row) -> bool:
    try:
        capability = json.loads(row["runtime_capability_json"])
        return bool(peer_runtime_is_activation_eligible(row["peer_class"], capability))
    except Exception:
        return False


def _record_projection(row: sqlite3.Row, *, now: float) -> dict[str, Any]:
    return {
        "protocol": ROUTE_ASSIGNMENT_RECORD_PROTOCOL,
        "node_id": row["node_id"],
        "assignment_id": row["assignment_id"],
        "endpoint_id": row["endpoint_id"],
        "stage_pack_digest": row["stage_pack_digest"],
        "peer_endpoint_id": row["peer_endpoint_id"],
        "route_generation": int(row["route_generation"]),
        "forward_to": bool(row["forward_to"]),
        "issued_at": float(row["issued_at"]),
        "expires_at": float(row["expires_at"]),
        "state": (
            "withdrawn"
            if row["withdrawn"]
            else "expired"
            if now >= float(row["expires_at"])
            else "applied"
            if row["applied_at"] is not None
            else "delivered"
            if row["delivered_at"] is not None
            else "issued"
        ),
        "delivered_at": row["delivered_at"],
        "delivered_generation": row["delivered_generation"],
        "applied_at": row["applied_at"],
        "applied_generation": row["applied_generation"],
        "route_ready": False,
    }


def load_stage_pack(path: Path) -> dict[str, Any]:
    """Read a stage pack (or a fixture carrying one under ``pack``) and verify it."""

    raw = Path(path).read_bytes()
    if len(raw) > 16 * MAX_STAGE_PACK_BYTES:
        raise _fail("seed_route_assignment_stage_pack_too_large")
    document = json.loads(raw)
    if isinstance(document, dict) and "pack" in document and "protocol" not in document:
        document = document["pack"]
    return verify_stage_pack(document)


def verify_stage_pack(document: Any) -> dict[str, Any]:
    try:
        PixelStage.from_document(document)
    except PixelStageError as exc:
        raise _fail(f"seed_route_assignment_stage_pack_invalid:{exc}") from exc
    if len(canonical_json_bytes(document)) > MAX_STAGE_PACK_BYTES:
        raise _fail("seed_route_assignment_stage_pack_too_large")
    return json.loads(canonical_json_bytes(document))


def issue_route_assignment(
    database: Path,
    *,
    node_id: str,
    stage_pack: Mapping[str, Any],
    peer_endpoint_id: str,
    route_generation: int,
    forward_to: bool = True,
    ttl_seconds: float = DEFAULT_ASSIGNMENT_TTL_SECONDS,
    now: Callable[[], float] = time.time,
    id_source: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Record (or replace) the one route assignment of an enrolled native member.

    Only a current, activation-eligible ``android_native_iroh`` member that has not
    been revoked can be assigned; the assignment is bound to its enrolled endpoint
    and member key so a re-enrolled device never inherits it.
    """

    if not _segment(node_id):
        raise _fail("seed_route_assignment_node_invalid")
    if not isinstance(peer_endpoint_id, str) or _ENDPOINT_RE.fullmatch(peer_endpoint_id) is None:
        raise _fail("seed_route_assignment_peer_invalid")
    if isinstance(route_generation, bool) or not isinstance(route_generation, int) or route_generation < 1:
        raise _fail("seed_route_assignment_generation_invalid")
    if not isinstance(forward_to, bool):
        raise _fail("seed_route_assignment_forward_invalid")
    ttl = _number(ttl_seconds)
    if ttl is None or not 0 < ttl <= MAX_ASSIGNMENT_TTL_SECONDS:
        raise _fail("seed_route_assignment_ttl_invalid")
    pack = verify_stage_pack(stage_pack)
    issued_at = float(now())
    assignment_id = (id_source or (lambda: f"route-{secrets.token_hex(8)}"))()
    if not _segment(assignment_id):
        raise _fail("seed_route_assignment_id_invalid")
    _ensure_schema(database)
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            member = _member_row(connection, node_id)
            if member is None:
                raise _fail("seed_member_unknown")
            if member["peer_class"] != _ANDROID_PEER_CLASS or not _member_eligible(member):
                raise _fail("seed_member_activation_ineligible")
            if member["lifecycle_state"] == "STOPPED":
                raise _fail("seed_member_revoked")
            if member["endpoint_id"] == peer_endpoint_id:
                raise _fail("seed_route_assignment_peer_invalid")
            connection.execute(
                """
                INSERT OR REPLACE INTO seed_route_assignments (
                    node_id, assignment_id, endpoint_id, verification_key_digest,
                    stage_pack_json, stage_pack_digest, peer_endpoint_id,
                    route_generation, forward_to, issued_at, expires_at, withdrawn,
                    delivered_at, delivered_generation, applied_at, applied_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL)
                """,
                (
                    node_id,
                    assignment_id,
                    member["endpoint_id"],
                    member["verification_key_digest"],
                    canonical_json_bytes(pack).decode("utf-8"),
                    pack["pack_digest"],
                    peer_endpoint_id,
                    route_generation,
                    1 if forward_to else 0,
                    issued_at,
                    issued_at + ttl,
                ),
            )
            row = connection.execute(
                "SELECT * FROM seed_route_assignments WHERE node_id = ?", (node_id,)
            ).fetchone()
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return _record_projection(row, now=issued_at)
    except sqlite3.Error as exc:
        raise _fail("seed_route_assignment_store_unavailable") from exc
    finally:
        connection.close()


def withdraw_route_assignment(
    database: Path, *, node_id: str, now: Callable[[], float] = time.time
) -> dict[str, Any]:
    _ensure_schema(database)
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE seed_route_assignments SET withdrawn = 1 WHERE node_id = ?", (node_id,)
            )
            row = connection.execute(
                "SELECT * FROM seed_route_assignments WHERE node_id = ?", (node_id,)
            ).fetchone()
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise _fail("seed_route_assignment_store_unavailable") from exc
    finally:
        connection.close()
    if row is None:
        raise _fail("seed_route_assignment_unknown")
    return _record_projection(row, now=float(now()))


def route_assignment_status(
    database: Path, *, node_id: str, now: Callable[[], float] = time.time
) -> dict[str, Any]:
    _ensure_schema(database)
    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT * FROM seed_route_assignments WHERE node_id = ?", (node_id,)
        ).fetchone()
    except sqlite3.Error as exc:
        raise _fail("seed_route_assignment_store_unavailable") from exc
    finally:
        connection.close()
    if row is None:
        raise _fail("seed_route_assignment_unknown")
    return _record_projection(row, now=float(now()))


def newest_android_member(database: Path, *, now: Callable[[], float] = time.time) -> str:
    """The single lease-fresh, unrevoked Android member (the gate's phone)."""

    connection = _connect(database)
    try:
        rows = connection.execute(
            """
            SELECT node_id FROM seed_members
            WHERE peer_class = ? AND lifecycle_state != 'STOPPED' AND lease_expires_at > ?
            ORDER BY generation DESC, node_id
            """,
            (_ANDROID_PEER_CLASS, float(now())),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _fail("seed_route_assignment_store_unavailable") from exc
    finally:
        connection.close()
    if len(rows) != 1:
        raise _fail("seed_route_assignment_member_ambiguous" if rows else "seed_member_unknown")
    return str(rows[0]["node_id"])


@dataclass
class RouteAssignmentService:
    """Answer one member's signed route-assignment request (runs inside the seed)."""

    database: Path
    signer: Any
    swarm_id: str
    seed_node_id: str
    incarnation: str
    clock: Callable[[], float] = time.time
    id_source: Callable[[], str] | None = None

    def __post_init__(self) -> None:
        _ensure_schema(self.database)

    def answer(self, envelope: Any) -> dict[str, Any]:
        now = float(self.clock())
        if not isinstance(envelope, Mapping) or set(envelope) != _ENVELOPE_FIELDS:
            raise _fail("seed_message_malformed")
        message, signature, record = (
            envelope["message"],
            envelope["signature"],
            envelope["verification_key"],
        )
        if (
            envelope["protocol"] != SIGNED_MESSAGE_PROTOCOL
            or not isinstance(message, Mapping)
            or not isinstance(signature, Mapping)
            or not isinstance(record, Mapping)
            or set(message) != _REQUEST_FIELDS
            or message["protocol"] != ROUTE_ASSIGNMENT_REQUEST_PROTOCOL
        ):
            raise _fail("seed_message_malformed")
        for field in (
            "message_id",
            "sender_node_id",
            "sender_endpoint_id",
            "recipient_node_id",
            "incarnation",
        ):
            if not _segment(message[field]):
                raise _fail("seed_message_malformed")
        applied = message["applied_assignment_id"]
        if applied is not None and not _segment(applied):
            raise _fail("seed_message_malformed")
        generation = message["generation"]
        issued_at, expires_at = _number(message["issued_at"]), _number(message["expires_at"])
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or issued_at is None
            or expires_at is None
            or not 0 < expires_at - issued_at <= MAX_REQUEST_TTL_SECONDS
        ):
            raise _fail("seed_message_malformed")
        if message["swarm_id"] != self.swarm_id or message["recipient_node_id"] != self.seed_node_id:
            raise _fail("seed_message_mismatch")
        if now > expires_at:
            raise _fail("membership_message_expired")
        if now + MAX_CLOCK_SKEW_SECONDS < issued_at:
            raise _fail("membership_message_from_future")
        node_id = message["sender_node_id"]
        connection = _connect(self.database)
        try:
            # One transaction: the member checks, the replay record and the
            # delivery bookkeeping commit together or not at all.
            connection.execute("BEGIN IMMEDIATE")
            member = _member_row(connection, node_id)
            if member is None:
                raise _fail("seed_member_unknown")
            if record.get("verification_key_digest") != member["verification_key_digest"]:
                raise _fail("membership_key_pin_mismatch")
            try:
                verify = build_ed25519_verifier([dict(record)])
                valid = verify(canonical_json_bytes(dict(message)), dict(signature))
            except Exception:
                valid = False
            if not valid:
                raise _fail("membership_signature_invalid")
            if (
                message["sender_endpoint_id"] != member["endpoint_id"]
                or message["incarnation"] != member["incarnation"]
                or generation != int(member["generation"])
            ):
                raise _fail("seed_state_member_stale")
            if member["lifecycle_state"] == "STOPPED":
                raise _fail("seed_member_revoked")
            if now >= float(member["lease_expires_at"]):
                raise _fail("seed_member_lease_expired")
            # Replay protection shared with every other member message: one
            # message id per member generation, kept until the request expires.
            connection.execute("DELETE FROM seed_replay WHERE expires_at < ?", (now,))
            (window,) = connection.execute(
                "SELECT COUNT(*) FROM seed_replay WHERE node_id = ? AND generation = ?",
                (node_id, generation),
            ).fetchone()
            if int(window) >= MAX_REPLAY_IDS:
                raise _fail("seed_replay_window_full")
            try:
                connection.execute(
                    "INSERT INTO seed_replay (node_id, generation, message_id, expires_at) VALUES (?, ?, ?, ?)",
                    (node_id, generation, message["message_id"], expires_at),
                )
            except sqlite3.IntegrityError as exc:
                raise _fail("seed_message_replayed") from exc
            row = connection.execute(
                """
                SELECT * FROM seed_route_assignments
                WHERE node_id = ? AND withdrawn = 0 AND expires_at > ?
                  AND endpoint_id = ? AND verification_key_digest = ?
                """,
                (node_id, now, member["endpoint_id"], member["verification_key_digest"]),
            ).fetchone()
            if row is not None and not _member_eligible(member):
                row = None
            if row is not None:
                # The member's own word on what it runs: a restarted node that
                # has not re-applied the assignment yet is not counted as applied.
                applied_now = applied == row["assignment_id"]
                connection.execute(
                    """
                    UPDATE seed_route_assignments
                    SET applied_at = ?, applied_generation = ?
                    WHERE node_id = ? AND assignment_id = ?
                    """,
                    (
                        now if applied_now else None,
                        generation if applied_now else None,
                        node_id,
                        row["assignment_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE seed_route_assignments
                    SET delivered_at = ?, delivered_generation = ?
                    WHERE node_id = ? AND assignment_id = ?
                    """,
                    (now, generation, node_id, row["assignment_id"]),
                )
            connection.execute("COMMIT")
        except BaseException as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise _fail("seed_route_assignment_store_unavailable") from exc
            raise
        finally:
            connection.close()
        assignment = None
        stage_pack = None
        statement_expires = now + DELIVERY_TTL_SECONDS
        if row is not None:
            assignment = {
                "assignment_id": row["assignment_id"],
                "stage_pack_digest": row["stage_pack_digest"],
                "peer_endpoint_id": row["peer_endpoint_id"],
                "route_generation": int(row["route_generation"]),
                "forward_to": bool(row["forward_to"]),
                "expires_at": float(row["expires_at"]),
            }
            stage_pack = json.loads(row["stage_pack_json"])
            statement_expires = min(statement_expires, float(row["expires_at"]))
        statement = {
            "protocol": ROUTE_ASSIGNMENT_PROTOCOL,
            "message_id": (
                self.id_source() if self.id_source is not None
                else f"route-delivery-{secrets.token_hex(8)}"
            ),
            "swarm_id": self.swarm_id,
            "sender_node_id": self.seed_node_id,
            "sender_endpoint_id": self.signer.endpoint_id,
            "recipient_node_id": node_id,
            "recipient_endpoint_id": member["endpoint_id"],
            "incarnation": self.incarnation,
            "generation": generation,
            "request_message_id": message["message_id"],
            "issued_at": now,
            "expires_at": statement_expires,
            "assignment": assignment,
            "route_ready": False,
        }
        signed = {
            "protocol": SIGNED_MESSAGE_PROTOCOL,
            "message": statement,
            "signature": self.signer.sign(statement),
            "verification_key": self.signer.public_key_record(),
        }
        return {
            "protocol": ROUTE_ASSIGNMENT_DELIVERY_PROTOCOL,
            "envelope": signed,
            "stage_pack": stage_pack,
        }


def service_for(coordinator: Any) -> RouteAssignmentService:
    """The route-assignment service of one running seed coordinator."""

    state = getattr(coordinator, "_state", None)
    if state is None:
        raise _fail("seed_route_assignment_store_unavailable")
    return RouteAssignmentService(
        database=Path(state.database),
        signer=coordinator.signer,
        swarm_id=coordinator.swarm_id,
        seed_node_id=coordinator.seed_node_id,
        incarnation=coordinator.incarnation,
        clock=coordinator._now,  # noqa: SLF001 - same package; keeps the seed's clock
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mycelium_seed.route_assignment",
        description="Owner command: issue, inspect or withdraw a member's route assignment.",
    )
    parser.add_argument("--state-root", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue")
    issue.add_argument("--node-id", help="default: the single lease-fresh Android member")
    issue.add_argument("--stage-pack", required=True, type=Path)
    issue.add_argument("--peer-endpoint-id", required=True)
    issue.add_argument("--route-generation", required=True, type=int)
    issue.add_argument("--no-forward", action="store_true")
    issue.add_argument("--ttl-seconds", type=float, default=DEFAULT_ASSIGNMENT_TTL_SECONDS)
    for name in ("status", "withdraw"):
        command = sub.add_parser(name)
        command.add_argument("--node-id")
        if name == "status":
            command.add_argument(
                "--wait-applied",
                type=float,
                default=0.0,
                help="seconds to wait until the member reports the assignment applied",
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Owner command. Like every seed operator command it opens the state root
    through ``_load_authority``: the private-directory lease (owner-only 0700
    directory, no symlink or rename swap, revalidated before each write), the
    owner-only database file and the seed key bound to the stored identity.

    The lease is a descriptor binding, not an exclusive lock, so the command
    runs next to the live seed. Concurrent writers are serialized by SQLite:
    every write here is one ``BEGIN IMMEDIATE`` transaction with a 30 s busy
    timeout, and the seed reads the assignment table fresh on each request.
    """

    args = _parser().parse_args(argv)
    try:
        lease, _coordinator, _binding = _load_authority(
            args.state_root, incarnation="seed-operator-route-assignment"
        )
    except SeedOperatorError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 2
    try:
        database = lease.path / "state.sqlite3"
        node_id = args.node_id or newest_android_member(database)
        lease.revalidate()
        if args.command == "issue":
            result = issue_route_assignment(
                database,
                node_id=node_id,
                stage_pack=load_stage_pack(args.stage_pack),
                peer_endpoint_id=args.peer_endpoint_id,
                route_generation=args.route_generation,
                forward_to=not args.no_forward,
                ttl_seconds=args.ttl_seconds,
            )
        elif args.command == "withdraw":
            result = withdraw_route_assignment(database, node_id=node_id)
        else:
            deadline = time.monotonic() + max(0.0, args.wait_applied)
            while True:
                lease.revalidate()
                result = route_assignment_status(database, node_id=node_id)
                if result["state"] == "applied" or time.monotonic() >= deadline:
                    break
                time.sleep(1.0)
            if args.wait_applied and result["state"] != "applied":
                print(json.dumps(result, sort_keys=True))
                return 1
    except SeedCoordinatorError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 1
    except ValueError:
        print(json.dumps({"error": "seed_operator_state_invalid"}), file=sys.stderr)
        return 2
    finally:
        lease.close()
    print(json.dumps(result, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
