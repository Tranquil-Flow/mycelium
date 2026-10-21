# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded in-memory retention of exact signed managed-control requests.

The production client caches the exact signed bytes for every enqueue/poll/ack/
result so an ambiguous transport failure replays byte-identical requests.  That
cache previously grew without bound: ordinary polling inserted one entry per
request and nothing ever removed them.  These tests reproduce the leak with the
real canonical TLS fixtures and real fixture signing, then verify bounded
capacity with explicit terminal forgetting -- never freshness refresh, sequence/
nonce reuse, lost unresolved commands, or broken durable caller replay.
"""
from collections.abc import Mapping
import time

import pytest

from test_managed_control import _world, _enqueue, _command, _native_response, RUN_ID, DEPLOYMENT_ID
from test_control_pump import _world as _pump_world
from test_control_session import owner, session, connected
from test_worker import worker

from mycelium_a13_macos import durable_requests
from mycelium_a13_macos.managed_control import (
    ManagedControlClient,
    ManagedControlError,
)
from mycelium_a13_macos.control_pump import TargetControlPump, ControlPumpError
from physical_inference_qualification import ControllerError


def _cache(client):
    return client._prepared_requests


def _target_kwargs(sequence):
    return dict(
        run_id=RUN_ID,
        deployment_id=DEPLOYMENT_ID,
        sequence=sequence,
        nonce=f"retention-nonce-{sequence}",
        request_id=f"retention-request-{sequence}",
    )


def test_high_count_polling_bounds_prepared_request_cache(tmp_path):
    """Real TLS polls with fresh request IDs must not grow the cache forever."""
    with _world(tmp_path / "swarm") as world:
        client = world["target_client"]
        for sequence in range(1, 301):
            world["target_client"].poll(
                target=world["member"], ttl_seconds=60.0, **_target_kwargs(sequence)
            )
        assert len(_cache(client)) <= client.max_prepared_requests
        assert len(_cache(client)) > 0


def test_prepared_request_capacity_is_validated_and_fifo_bounded(tmp_path):
    with _world(tmp_path / "swarm") as world:
        for bad in (1, 0, -1, True, "256", 1.5, None):
            with pytest.raises(ManagedControlError) as error:
                ManagedControlClient.for_local_tls(
                    base_url=world["server"].base_url,
                    signer=world["owner_signer"],
                    trusted_owner_key_record=world["owner_signer"].public_key_record(),
                    swarm_id=world["authority"].coordinator.swarm_id,
                    owner_node_id=world["authority"].coordinator.seed_node_id,
                    ca_certificate=world["authority"].seed_server.material.ca_certificate,
                    max_prepared_requests=bad,
                )
            assert error.value.code == "managed_control_prepared_capacity_invalid"
        # Capacity 2 keeps exactly the two most recent request ids (FIFO).
        client = ManagedControlClient.for_local_tls(
            base_url=world["server"].base_url,
            signer=world["owner_signer"],
            trusted_owner_key_record=world["owner_signer"].public_key_record(),
            swarm_id=world["authority"].coordinator.swarm_id,
            owner_node_id=world["authority"].coordinator.seed_node_id,
            ca_certificate=world["authority"].seed_server.material.ca_certificate,
            max_prepared_requests=2,
        )
        assert client.max_prepared_requests == 2
        for sequence in (1, 2, 3):
            client.enqueue(
                target=world["member"],
                native_command=_command(f"fifo-isolated-{sequence}"),
                sequence=sequence,
                nonce=f"fifo-isolated-nonce-{sequence}",
                request_id=f"fifo-isolated-request-{sequence}",
            )
        assert len(_cache(client)) == 2
        assert set(_cache(client)) == {
            "fifo-isolated-request-2",
            "fifo-isolated-request-3",
        }


def test_forget_prepared_request_and_deterministic_resign_after_forget(tmp_path):
    """Explicit forgetting removes bytes; Ed25519 deterministic re-sign of the
    same fixed-time statement reproduces identical bytes (fresh-window refresh
    is what must NOT happen implicitly)."""
    with _world(tmp_path / "swarm") as world:
        client = world["owner_client"]
        statement = client.owner_command_statement(
            target=world["member"],
            native_command=_command("forget-resign"),
            sequence=1,
            nonce="forget-nonce-1",
            request_id="forget-request-1",
        )
        first = client._signed_outbound(dict(statement))
        assert "forget-request-1" in client.prepared_request_ids()
        client.forget_prepared_request("forget-request-1")
        assert "forget-request-1" not in client.prepared_request_ids()
        assert "forget-request-1" not in _cache(client)
        # Re-preparing the same statement deterministically re-signs the
        # identical canonical bytes: signature is a pure function of statement.
        second = client._signed_outbound(dict(statement))
        assert first == second
        assert second["statement"]["issued_at"] == statement["issued_at"]
        assert second["statement"]["expires_at"] == statement["expires_at"]


def test_lost_poll_reply_replays_exact_bytes_and_keeps_single_claim(tmp_path):
    with _pump_world(tmp_path / "swarm") as world:
        root = tmp_path / "native"
        root.mkdir()
        w = worker(
            root,
            node_id=world["member"]["node_id"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
        )
        state = tmp_path / "pump"
        current = lambda: world["authority"].coordinator.member(world["member"]["node_id"])  # noqa: E731
        pump = TargetControlPump(
            state,
            client=world["target_client"],
            worker=w,
            current_member=current,
        )
        original_request = world["target_client"]._request
        sent = []
        original_poll = world["target_client"].poll

        def observe(path, envelope):
            if path.endswith("/poll"):
                sent.append(envelope)
            return original_request(path, envelope)

        def lose(**kwargs):
            original_poll(**kwargs)
            raise TimeoutError("poll response deliberately lost after server commit")

        try:
            w.start()
            _enqueue(world, "retention-lost-poll")
            world["target_client"]._request = observe
            world["target_client"].poll = lose
            with pytest.raises((ControlPumpError, TimeoutError)):
                pump.step()
            assert w.status()["command_count"] == 0
            # Byte-identical replay of the persisted envelope after the failure,
            # under the retention bound.
            world["target_client"].poll = original_poll
            recovered = pump.step()
            assert recovered["state"] == "completed"
            assert recovered["native_response"]["ok"] is True
            assert w.status()["command_count"] == 1
            assert sent[0] == sent[1]  # exact same signed bytes both attempts
            assert len(_cache(world["target_client"])) <= world["target_client"].max_prepared_requests
        finally:
            pump.close()
            w.close()
        assert not w.process_group_present()


def test_durable_owner_journal_restart_replays_exact_envelope_within_bound(tmp_path, monkeypatch):
    with connected(tmp_path) as (world, w):
        sender = owner(tmp_path / "sender", world)
        original = world["owner_client"].enqueue
        # Saturate the in-memory cache locally through the exact production
        # signing path (no HTTP), so the shared signer's server-side sequence
        # space stays intact for the concurrently polling pump.
        for index in range(1, 40):
            world["owner_client"]._signed_outbound(
                world["owner_client"].owner_command_statement(
                    target=world["member"],
                    native_command=_command(f"saturation-{index}"),
                    sequence=index,
                    nonce=f"saturation-nonce-{index}",
                    request_id=f"saturation-request-{index}",
                )
            )
        assert len(_cache(world["owner_client"])) == 39
        try:
            def lose(**kwargs):
                original(**kwargs)
                raise TimeoutError("deliberately dropped successful enqueue response")

            monkeypatch.setattr(world["owner_client"], "enqueue", lose)
            with pytest.raises(ControllerError, match="reconciliation_required"):
                session(sender, world).send(command_id="bound-lost-enqueue", command="hello", payload={})
            sender.close()
            monkeypatch.setattr(world["owner_client"], "enqueue", original)
            # Fresh interpreter: no volatile cache; durable replay must re-seed it.
            world["owner_client"]._prepared_requests.clear()
            sender = owner(tmp_path / "sender", world)
            reply = session(sender, world).send(command_id="bound-lost-enqueue", command="hello", payload={})
            assert reply["ok"] is True
            assert w.status()["command_count"] == 1
            # Optional parent hook: terminal forgetting after confirmed enqueue.
            for row in sender._state["commands"].values():
                if row.get("enqueue_confirmed"):
                    durable_requests.forget(sender.client, row["enqueue_request"])
            assert len(_cache(world["owner_client"])) <= world["owner_client"].max_prepared_requests
            assert len(_cache(world["owner_client"])) > 0
        finally:
            sender.close()


def test_conflicting_intent_for_same_request_id_fails_closed(tmp_path):
    with _world(tmp_path / "swarm") as world:
        client = world["owner_client"]
        _enqueue(world, "conflict-seed")
        cached = _cache(client)["owner-request-1"]
        tampered_intent = dict(cached[0])
        tampered_intent["native_command"] = _command("conflict-seed", "health")
        client._prepared_requests["owner-request-1"] = (tampered_intent, cached[1])
        with pytest.raises(ManagedControlError, match="managed_control_conflicting_retry"):
            _enqueue(world, "conflict-seed")


def test_prepare_reuses_exact_signed_bytes_without_refreshing_freshness(tmp_path):
    with _world(tmp_path / "swarm") as world:
        client = world["owner_client"]
        statement = client.owner_command_statement(
            target=world["member"],
            native_command=_command("reuse-bytes"),
            sequence=1,
            nonce="reuse-nonce-1",
            request_id="reuse-request-1",
        )
        first = client._signed_outbound(dict(statement))
        # Same request, new attempt: identical bytes; no re-issued freshness window.
        second = client._signed_outbound(dict(statement))
        assert first == second
        assert first["statement"]["issued_at"] == statement["issued_at"]
        assert first["statement"]["expires_at"] == statement["expires_at"]
        # Forgetting then retrying still reproduces the exact same bytes: the
        # cache never refreshes a signed freshness window.
        client.forget_prepared_request("reuse-request-1")
        third = client._signed_outbound(dict(statement))
        assert first == third
