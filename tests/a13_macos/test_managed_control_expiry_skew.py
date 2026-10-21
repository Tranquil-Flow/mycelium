# SPDX-License-Identifier: AGPL-3.0-only
"""A member whose clock runs ahead must still accept short-TTL owner commands (live demo RCA, 2026-10-20).

The laptop's clock was 9.19 s ahead of the owner's.  ``_validate_time`` applied ``MAX_CLOCK_SKEW_SECONDS`` only to
the "from the future" side, so the member rejected every owner command whose TTL was below the offset as
``managed_control_message_expired`` -- ``health`` (0.5 s), ``snapshot`` (4 s) and ``infer_cancel_wait`` (4.5 s, the cleanup
window): 0 of 984 short-TTL commands completed, the cleanup was never receipted, and every chat blocked with
``command_cleanup_receipt_missing``.  The documented 30 s tolerance now holds on the expiry side too; replay stays
blocked by the nonce and sequence ledgers, and the owner's mailbox still expires rows on its own clock.
"""
import time

import pytest

from mycelium_a13_macos.managed_control import (
    MAX_CLOCK_SKEW_SECONDS,
    ManagedControlClient,
    ManagedControlError,
    _validate_time,
)

T = 1_000_000.0


def test_a_member_clock_nine_seconds_ahead_does_not_expire_a_4_5_second_command():
    statement = {"issued_at": T, "expires_at": T + 4.5}
    _validate_time(statement, now=T + 4.5 + 9.19)  # must not raise
    _validate_time(statement, now=T + 4.5 + MAX_CLOCK_SKEW_SECONDS)  # the edge of the tolerance


def test_a_command_expired_beyond_the_skew_tolerance_is_still_expired():
    with pytest.raises(ManagedControlError, match="managed_control_message_expired"):
        _validate_time({"issued_at": T, "expires_at": T + 4.5}, now=T + 4.5 + 31)


def test_the_from_the_future_side_is_unchanged():
    _validate_time({"issued_at": T + MAX_CLOCK_SKEW_SECONDS, "expires_at": T + 60}, now=T)
    with pytest.raises(ManagedControlError, match="managed_control_message_from_future"):
        _validate_time({"issued_at": T + 31, "expires_at": T + 90}, now=T)


def test_a_skewed_member_pump_completes_a_short_ttl_owner_command_without_an_uncertain_ack(tmp_path):
    from test_control_pump import _world
    from test_managed_control import DEPLOYMENT_ID, RUN_ID, _command
    from test_worker import worker
    from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump

    with _world(tmp_path / "swarm") as world:
        authority = world["authority"]
        world["target_client"] = ManagedControlClient.for_local_tls(
            signer=world["target_signer"], base_url=authority.seed_server.base_url,
            trusted_owner_key_record=world["owner_signer"].public_key_record(),
            swarm_id=authority.coordinator.swarm_id, owner_node_id=authority.coordinator.seed_node_id,
            ca_certificate=authority.seed_server.material.ca_certificate,
            clock=lambda: time.time() + 9.19)  # the laptop: 9.19 s ahead of the owner
        root = tmp_path / "native"
        root.mkdir()
        w = worker(root, node_id=world["member"]["node_id"], run_id=RUN_ID, deployment_id=DEPLOYMENT_ID)
        pump = ConcurrentTargetControlPump(
            tmp_path / "pump", client=world["target_client"], worker=w,
            current_member=lambda: authority.coordinator.member(world["member"]["node_id"]))
        try:
            w.start()
            world["owner_client"].enqueue(
                target=world["member"], native_command=_command("short-ttl-cleanup"), sequence=1,
                nonce="owner-nonce-1", request_id="owner-request-1", ttl_seconds=4.5)
            deadline = time.monotonic() + 8
            result = {}
            while time.monotonic() < deadline:
                result = pump.step()
                if result.get("command_id") == "short-ttl-cleanup" and result["state"] == "completed":
                    break
                time.sleep(0.01)
            assert result.get("state") == "completed", result
            assert result["native_response"]["ok"] is True
            assert w.status()["command_count"] == 1
            assert not (tmp_path / "pump" / "uncertain-ack.private.json").exists()
        finally:
            pump.close()
            w.close()
