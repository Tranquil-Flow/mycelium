# SPDX-License-Identifier: AGPL-3.0-only
"""PV26: peer clock offset from signed message timestamps vs receipt time, RTT-corrected.

The demo failure this exists for: a joined Mac's clock ran 9.2 s ahead, every short-TTL command
expired, and nothing on screen said so. The estimator is pure; the probes only READ the
timestamps the control protocol already carries (they change no protocol behaviour).
"""
from __future__ import annotations

import time

import pytest

from mycelium_a13_macos.diagnostics.clock import (
    ClockSamples,
    estimate_offset,
    install_client_probe,
    install_mailbox_probe,
    uninstall_client_probe,
)
from mycelium_a13_macos.diagnostics.state import DiagState
from mycelium_a13_macos.managed_control import ManagedControlClient

from test_managed_control import DEPLOYMENT_ID, RUN_ID, _enqueue, _world

SKEW = 9.19


# -- pure estimator -------------------------------------------------------------------------------

def test_estimate_is_rtt_corrected_and_prefers_the_least_delayed_sample():
    # true offset +9.2 s; a sample carries (offset - one-way delay)
    samples = [(9.2 - 0.35, 0.0), (9.2 - 0.12, 0.0), (9.2 - 0.20, 0.0)]
    out = estimate_offset([(remote, local) for remote, local in samples], rtt_s=0.24)
    assert out["available"] is True
    assert out["offset_s"] == pytest.approx(9.2, abs=0.01)  # best raw 9.08 + rtt/2
    assert out["rtt_corrected"] is True
    assert out["uncertainty_s"] == pytest.approx(0.12, abs=0.01)
    assert out["direction"] == "ahead" and out["samples"] == 3


def test_without_an_rtt_the_estimate_says_it_is_not_corrected():
    out = estimate_offset([(8.9, 0.0), (9.0, 0.0)])
    assert out["offset_s"] == pytest.approx(9.0)
    assert out["rtt_corrected"] is False and out["uncertainty_s"] is None


def test_negative_offset_is_behind_and_small_is_in_sync():
    assert estimate_offset([(-4.0, 0.0)], rtt_s=0.0)["direction"] == "behind"
    assert estimate_offset([(0.05, 0.0)], rtt_s=0.1)["direction"] == "in_sync"


def test_no_samples_is_not_available_with_a_reason():
    out = estimate_offset([])
    assert out["available"] is False and "no signed message" in out["reason"]


def test_samples_older_than_the_window_are_forgotten():
    now = [1000.0]
    store = ClockSamples(clock=lambda: now[0], window_s=60.0)
    store.add("peer-a", remote_ts=1009.0, local_ts=1000.0)
    now[0] = 1100.0
    assert store.estimate("peer-a")["available"] is False
    store.add("peer-a", remote_ts=1109.5, local_ts=1100.0)
    assert store.estimate("peer-a")["offset_s"] == pytest.approx(9.5)


def test_store_is_bounded_per_peer():
    store = ClockSamples(clock=lambda: 0.0, maxlen=8)
    for i in range(100):
        store.add("p", remote_ts=float(i), local_ts=0.0)
    assert store.estimate("p")["samples"] == 8


# -- probes on the real signed protocol (loopback TLS, real mailbox) ---------------------------------

def _skewed_client(world, skew):
    authority = world["authority"]
    return ManagedControlClient.for_local_tls(
        base_url=world["server"].base_url,
        signer=world["target_signer"],
        trusted_owner_key_record=world["owner_signer"].public_key_record(),
        swarm_id=authority.coordinator.swarm_id,
        owner_node_id=authority.coordinator.seed_node_id,
        clock=lambda: time.time() + skew,
        ca_certificate=authority.seed_server.material.ca_certificate,
    )


def _poll(world, client, n, tag=""):
    return client.poll(target=world["member"], run_id=RUN_ID, deployment_id=DEPLOYMENT_ID,
                       sequence=n, nonce=f"diag-nonce-{n}{tag}", request_id=f"diag-poll-{n}{tag}")


def test_owner_estimates_a_member_clock_that_runs_9_seconds_fast(tmp_path):
    state = DiagState()
    with _world(tmp_path, mailbox_options={"long_poll_hold": 0.0}) as world:
        install_mailbox_probe(world["mailbox"], state)
        install_mailbox_probe(world["mailbox"], state)  # idempotent: never double-records
        client = _skewed_client(world, SKEW)
        for n in range(1, 5):
            assert _poll(world, client, n)["delivery_kind"] == "empty"
        node = world["member"]["node_id"]
        out = state.clock.estimate(node)
        assert out["available"] and out["direction"] == "ahead"
        assert out["offset_s"] == pytest.approx(SKEW, abs=0.5)
        assert out["samples"] == 4  # 4 polls, recorded once each


def test_a_refused_request_leaves_no_clock_sample(tmp_path):
    state = DiagState()
    with _world(tmp_path, mailbox_options={"long_poll_hold": 0.0}) as world:
        install_mailbox_probe(world["mailbox"], state)
        client = _skewed_client(world, SKEW)
        _poll(world, client, 1)
        with pytest.raises(Exception):
            _poll(world, client, 1, "-again")  # a replayed sequence is refused
        assert state.clock.estimate(world["member"]["node_id"])["samples"] == 1


@pytest.fixture
def client_probe():
    state = DiagState()
    yield state
    uninstall_client_probe()


def test_member_estimates_the_owner_clock_and_records_claim_loop_latency(tmp_path, client_probe):
    state = client_probe
    with _world(tmp_path, mailbox_options={"long_poll_hold": 0.0}) as world:
        client = _skewed_client(world, SKEW)  # this Mac is 9.19 s AHEAD, so the owner is 9.19 s behind
        _enqueue(world, "hello-diag-1")  # the owner's own client runs before the member probe goes in
        install_client_probe(state)
        delivery = _poll(world, client, 1)
        assert delivery["delivery_kind"] == "command"
        owner = world["authority"].coordinator.seed_node_id
        out = state.clock.estimate(owner)
        assert out["available"] and out["direction"] == "behind"
        assert out["offset_s"] == pytest.approx(-SKEW, abs=0.5)
        assert out["rtt_corrected"] is True
        loop = state.client.summary()
        assert loop["polls"] == 1 and loop["errors"] == {}
        assert loop["median_poll_seconds"] is not None and loop["median_poll_seconds"] < 5.0
        # the delivered command's remaining life, on the member's own clock
        assert loop["last_command"]["ttl_seconds"] > 0


def test_member_records_the_bounded_error_code_of_a_failed_poll(tmp_path, client_probe):
    state = client_probe
    with _world(tmp_path, mailbox_options={"long_poll_hold": 0.0}) as world:
        client = _skewed_client(world, SKEW)
        install_client_probe(state)
        _poll(world, client, 1)
        with pytest.raises(Exception):
            _poll(world, client, 1, "-again")
        loop = state.client.summary()
        assert loop["polls"] == 2 and sum(loop["errors"].values()) == 1
        assert all(code.startswith("managed_control_") for code in loop["errors"])


def test_uninstalling_restores_the_client_class():
    original = ManagedControlClient.poll
    state = DiagState()
    install_client_probe(state)
    assert ManagedControlClient.poll is not original
    uninstall_client_probe()
    assert ManagedControlClient.poll is original
