# SPDX-License-Identifier: AGPL-3.0-only
"""PV26: command mailbox health per peer, read from the owner mailbox opened READ-ONLY."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from mycelium_a13_macos.diagnostics.mailbox_health import mailbox_health, open_readonly

from test_managed_control import _world

DDL = """
CREATE TABLE managed_commands (
    command_id TEXT PRIMARY KEY NOT NULL, envelope_digest TEXT NOT NULL, envelope_json TEXT NOT NULL,
    target_node_id TEXT NOT NULL, target_endpoint_id TEXT NOT NULL, target_key_digest TEXT NOT NULL,
    membership_generation INTEGER NOT NULL, member_incarnation TEXT NOT NULL, run_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL, owner_sequence INTEGER NOT NULL, owner_nonce TEXT NOT NULL,
    command_expires_at REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','claimed','reconciliation_required','completed','failed','expired')),
    claim_token TEXT, claimed_request_id TEXT, claimed_at REAL, native_response_digest TEXT,
    native_response_json TEXT, terminal_receipt_json TEXT, updated_at REAL NOT NULL
) WITHOUT ROWID;
"""
NOW = 10_000.0


def _db(path, rows):
    with sqlite3.connect(path) as db:
        db.executescript(DDL)
        for i, (node, state, issued, ttl, claimed_after, done_after, op) in enumerate(rows):
            envelope = {"statement": {"issued_at": issued, "expires_at": issued + ttl,
                                      "native_command": {"command": op}}}
            claimed = None if claimed_after is None else issued + claimed_after
            updated = issued + (done_after if done_after is not None else ttl)
            db.execute("INSERT INTO managed_commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"c{i}", hashlib.sha256(str(i).encode()).hexdigest(), json.dumps(envelope), node,
                        "e", "k", 1, "inc", "run", "dep", i, f"n{i}", issued + ttl, state,
                        None, None, claimed, None, None, None, updated))


def test_counts_per_state_per_peer_and_the_9_second_skew_signature(tmp_path):
    path = tmp_path / "mailbox.sqlite3"
    rows = []
    # the joined Mac: every 0.5 s health expires unclaimed, snapshots end reconciliation_required
    rows += [("laptop", "expired", NOW - 300 + i, 0.5, None, None, "health") for i in range(20)]
    rows += [("laptop", "reconciliation_required", NOW - 200 + i, 4.0, 1.0, 2.0, "snapshot") for i in range(5)]
    rows += [("laptop", "completed", NOW - 100 + i, 60.0, 14.0, 15.0, "infer_start") for i in range(2)]
    # the owner's own node is healthy
    rows += [("owner", "completed", NOW - 100 + i, 0.5, 0.05, 0.1, "health") for i in range(10)]
    _db(path, rows)
    out = mailbox_health(path, now=NOW, window_s=600)
    assert out["available"] is True
    laptop = out["peers"]["laptop"]
    assert laptop["counts"]["expired"] == 20 and laptop["counts"]["reconciliation_required"] == 5
    assert laptop["counts"]["completed"] == 2 and laptop["counts"]["pending"] == 0
    assert laptop["total"] == 27
    assert laptop["short_ttl"]["total"] == 25 and laptop["short_ttl"]["completed"] == 0
    assert laptop["short_ttl"]["expired"] == 20
    assert laptop["median_claim_latency_s"] == pytest.approx(1.0)  # claimed rows: 5 at 1.0 s, 2 at 14 s
    owner = out["peers"]["owner"]
    assert owner["counts"]["completed"] == 10 and owner["median_claim_latency_s"] == pytest.approx(0.05)
    assert owner["short_ttl"]["completed"] == 10
    assert owner["min_round_trip_s"] == pytest.approx(0.05, abs=1e-6)  # claimed -> terminal on the fastest command


def test_only_the_last_n_minutes_count(tmp_path):
    path = tmp_path / "m.sqlite3"
    _db(path, [("a", "completed", NOW - 3600, 30.0, 0.1, 0.2, "hello"),
               ("a", "expired", NOW - 30, 30.0, None, None, "hello")])
    out = mailbox_health(path, now=NOW, window_s=300)
    assert out["peers"]["a"]["total"] == 1 and out["peers"]["a"]["counts"]["expired"] == 1


def test_missing_mailbox_is_not_available_with_a_reason(tmp_path):
    out = mailbox_health(tmp_path / "absent.sqlite3", now=NOW)
    assert out["available"] is False and "mailbox" in out["reason"]
    assert not (tmp_path / "absent.sqlite3").exists()  # read-only open never creates a database


def test_the_mailbox_is_opened_read_only_and_never_modified(tmp_path):
    path = tmp_path / "m.sqlite3"
    _db(path, [("a", "completed", NOW - 30, 30.0, 0.1, 0.2, "hello")])
    before = path.read_bytes()
    connection = open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE managed_commands SET state = 'expired'")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM managed_commands")
    finally:
        connection.close()
    mailbox_health(path, now=NOW)
    assert path.read_bytes() == before


def test_a_garbled_envelope_does_not_break_the_report(tmp_path):
    path = tmp_path / "m.sqlite3"
    _db(path, [("a", "completed", NOW - 30, 30.0, 0.1, 0.2, "hello")])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE managed_commands SET envelope_json = 'not json'")
    out = mailbox_health(path, now=NOW)
    assert out["available"] is True and out["peers"]["a"]["counts"]["completed"] == 1


def test_the_real_mailbox_schema_is_readable(tmp_path):
    with _world(tmp_path) as world:
        out = mailbox_health(world["database"], now=1.0e12)
        assert out["available"] is True and out["peers"] == {}
