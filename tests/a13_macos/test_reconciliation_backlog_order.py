# SPDX-License-Identifier: AGPL-3.0-only
"""V1.1 item j (finding): reconciliation demands are served ahead of new work, one poll + one ack each.

First demo RCA (2026-10-20): the laptop's claims for 60 s commands took 14-52 s while ~50 reconciliation round trips a
minute went by.  The mailbox rule behind it (``ManagedControlMailbox.poll``): while ANY claimed row is missing from the
poll's ``active_claims`` (or is awaiting a reconciliation ack) the poll delivers that row as a
``reconciliation_required`` demand **instead of** new work -- so each lost claim costs a member two HTTP round trips
(poll -> demand, ack) before a fresh command can be handed over.  Short-lived commands that the member never executes
(mailbox TTL 0.5 s for a health probe) therefore add a backlog that delays the real work behind them.

This characterization pins that cost model so a change to it is deliberate; the live fixes for the *source* of the
backlog are the expiry-skew tolerance (a late-but-valid command is executed, not dropped) and the longer idle-probe
lifetime for managed-control peers (``ManagedNodeSession.idle_probe_floor_seconds``).
"""
from __future__ import annotations

from pathlib import Path

from test_managed_control import (DEPLOYMENT_ID, RUN_ID, _concurrent_poll, _enqueue, _native_response, _world)


def test_each_lost_claim_costs_a_demand_poll_and_an_ack_before_fresh_work(tmp_path: Path) -> None:
    with _world(tmp_path, mailbox_options={"long_poll_hold": 0.0}) as world:
        member = world["member"]
        _enqueue(world, "health-1", sequence=1, operation="health")
        _enqueue(world, "health-2", sequence=2, operation="health")
        counter = {"seq": 0}

        def poll(active):
            counter["seq"] += 1
            n = counter["seq"]
            return _concurrent_poll(world, sequence=n, nonce=f"t-nonce-{n}", request_id=f"t-poll-{n}", active_claims=active)

        def ack(command_id, token):
            counter["seq"] += 1
            n = counter["seq"]
            return world["target_client"].ack(
                target=member, run_id=RUN_ID, deployment_id=DEPLOYMENT_ID, command_id=command_id, claim_token=token,
                disposition="reconciliation_required", native_response=None, sequence=n, nonce=f"t-nonce-{n}",
                request_id=f"t-ack-{n}")

        first = poll({})
        second = poll({first["command_id"]: first["claim_token"]})
        assert (first["delivery_kind"], second["delivery_kind"]) == ("command", "command")
        claims = {first["command_id"]: first["claim_token"], second["command_id"]: second["claim_token"]}

        _enqueue(world, "health-3", sequence=3, operation="health")   # fresh work, behind two claims the member then loses

        # The member restarted: it owes the owner two claims it no longer lists.
        requests = 0
        order = []
        while True:
            delivery = poll({})
            requests += 1
            order.append(delivery["delivery_kind"])
            if delivery["delivery_kind"] != "reconciliation_required":
                break
            ack(delivery["command_id"], delivery["claim_token"])
            requests += 1
            assert delivery["command_id"] in claims
        assert order == ["reconciliation_required", "reconciliation_required", "command"]
        assert delivery["command_id"] == "health-3"
        assert requests == 5   # two demands x (poll + ack) + the poll that finally carries the fresh command
