from __future__ import annotations

import itertools
import threading
import time
from typing import Any

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_interactive.swarm import (
    SwarmCoordinator,
    SwarmError,
    matrix_digest,
)
from mycelium_membership import JOIN_ACCEPTANCE_PROTOCOL, verify_membership_message
from mycelium_node import NodeMembershipSession
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator, SeedCoordinatorError, SqliteSeedState


class ManualClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def stage_pack() -> dict[str, Any]:
    return {
        "protocol": "mycelium.pixel_stage_pack.v1",
        "assignment_id": "assignment-browser",
        "stage_id": "browser-stage-001",
        "start_layer": 1,
        "end_layer_exclusive": 2,
        "hidden_size": 2,
        "pack_digest": "sha256:" + "a" * 64,
        "route_ready": False,
        "tensors": {"not_used_by_core": [1]},
    }


def coordinator(
    *,
    clock: ManualClock | None = None,
    wall_clock: ManualClock | None = None,
    seed_coordinator: SeedCoordinator | None = None,
    max_peers: int = 4,
    max_pending_jobs: int = 4,
    max_peer_history: int = 64,
    max_job_history: int = 256,
    peer_idle_ttl_seconds: float = 45.0,
) -> SwarmCoordinator:
    token_counter = itertools.count()
    id_counter = itertools.count()
    return SwarmCoordinator(
        stage_pack=stage_pack(),
        clock=clock or time.monotonic,
        wall_clock=wall_clock or time.time,
        seed_coordinator=seed_coordinator,
        token_source=lambda: f"secret-token-{next(token_counter):040d}",
        id_source=lambda prefix: f"{prefix}-{next(id_counter):04d}",
        max_peers=max_peers,
        invite_ttl_seconds=30.0,
        session_ttl_seconds=60.0,
        peer_idle_ttl_seconds=peer_idle_ttl_seconds,
        max_pending_jobs=max_pending_jobs,
        max_peer_history=max_peer_history,
        max_job_history=max_job_history,
    )


def durable_seed(
    tmp_path: Any,
    *,
    clock: ManualClock,
    signer: Any | None = None,
    id_prefix: str = "seed-message",
) -> SeedCoordinator:
    database = tmp_path / "seed-state" / "state.sqlite3"
    message_ids = itertools.count()
    return SeedCoordinator(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_url="https://seed.example.test",
        signer=signer or generate_ed25519_signer(endpoint_id="seed-endpoint"),
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-incarnation",
        clock=clock,
        id_source=lambda: f"{id_prefix}-{next(message_ids):04d}",
        lease_seconds=300.0,
    )


def join(swarm: SwarmCoordinator) -> tuple[str, str]:
    invite = swarm.create_invite(public_origin="https://swarm.example.test")
    grant = swarm.exchange_invite(invite.token)
    return grant.peer_id, grant.session_token


def valid_result(work: dict[str, Any], output: list[list[float]]) -> dict[str, Any]:
    return {
        "protocol": "mycelium.browser_stage_result.v1",
        "job_id": work["job_id"],
        "request_id": work["request_id"],
        "assignment_id": work["assignment_id"],
        "stage_id": work["stage_id"],
        "pack_digest": work["pack_digest"],
        "input_digest": work["input_digest"],
        "output": output,
        "output_digest": matrix_digest(output),
        "route_ready": False,
    }


def start_work(
    swarm: SwarmCoordinator,
    peer_id: str,
    token: str,
    work: dict[str, Any],
) -> bool:
    return swarm.start_work(
        peer_id=peer_id,
        session_token=token,
        job_id=work["job_id"],
        request_id=work["request_id"],
        input_digest=work["input_digest"],
    )


def test_browser_membership_is_signed_and_survives_seed_restart(tmp_path: Any) -> None:
    clock = ManualClock()
    clock.value = 2_000.0
    seed = durable_seed(tmp_path, clock=clock)
    swarm = coordinator(
        clock=clock,
        wall_clock=clock,
        seed_coordinator=seed,
    )

    invitation = swarm.create_invite(public_origin="https://swarm.example.test")
    grant = swarm.exchange_invite(invitation.token)
    acceptance = verify_membership_message(
        grant.membership_acceptance,
        now=clock.value,
        expected_key_digest=seed.signer.verification_key_digest,
        expected_protocol=JOIN_ACCEPTANCE_PROTOCOL,
    )
    assert acceptance["accepted_node_id"] == grant.peer_id
    assert acceptance["membership_generation"] == 1
    assert seed.member(grant.peer_id)["peer_class"] == "browser_http"

    restored_seed = durable_seed(
        tmp_path,
        clock=clock,
        signer=seed.signer,
        id_prefix="restored-seed-message",
    )
    restored = coordinator(
        clock=clock,
        wall_clock=clock,
        seed_coordinator=restored_seed,
    )
    peer = next(
        peer
        for peer in restored.status()["peers"]
        if peer["peer_id"] == grant.peer_id
    )
    assert peer["membership_generation"] == 1
    assert peer["peer_class"] == "browser_http"
    with pytest.raises(SwarmError, match="peer_unauthorized"):
        restored.poll_work(
            peer_id=grant.peer_id,
            session_token=grant.session_token,
            timeout_seconds=0,
        )


def test_browser_and_mac_members_cannot_collide_on_node_id(tmp_path: Any) -> None:
    clock = ManualClock()
    clock.value = 2_000.0
    seed = durable_seed(tmp_path, clock=clock)
    swarm = coordinator(
        clock=clock,
        wall_clock=clock,
        seed_coordinator=seed,
    )
    browser_id, _token = join(swarm)
    mac = NodeMembershipSession(
        node_id=browser_id,
        swarm_id=seed.swarm_id,
        seed_node_id=seed.seed_node_id,
        signer=generate_ed25519_signer(endpoint_id="mac-endpoint"),
        incarnation="mac-incarnation",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=clock,
        id_source=lambda: "mac-join-message",
    )
    bundle = seed.mint_invite(nonce="mac-collision", ttl_seconds=120)
    verified = verify_invite_bundle(bundle, now=clock.value)
    request = mac.join_request(
        invite_nonce=verified["payload"]["nonce"],
        endpoint_addrs=["https://mac.example.test/control"],
    )

    with pytest.raises(SeedCoordinatorError, match="seed_node_key_conflict"):
        seed.accept_join(invite_token=bundle["token"], join_envelope=request)
    assert seed.member(browser_id)["peer_class"] == "browser_http"


def test_revoked_in_flight_browser_is_fenced_by_membership_generation(
    tmp_path: Any,
) -> None:
    clock = ManualClock()
    clock.value = 2_000.0
    seed = durable_seed(tmp_path, clock=clock)
    swarm = coordinator(
        clock=clock,
        wall_clock=clock,
        seed_coordinator=seed,
    )
    peer_id, token = join(swarm)
    outcome: list[str] = []

    def dispatch() -> None:
        try:
            swarm.dispatch(
                request_id="generation-fenced",
                hidden=[[1.0, 2.0]],
                timeout_seconds=5,
            )
        except SwarmError as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=dispatch)
    thread.start()
    work = swarm.poll_work(
        peer_id=peer_id,
        session_token=token,
        timeout_seconds=1,
    )
    assert work is not None
    assert start_work(swarm, peer_id, token, work) is True
    generation = seed.member(peer_id)["generation"]
    revoked = seed.advance_member_generation(
        node_id=peer_id,
        expected_generation=generation,
        lifecycle_state="STOPPING",
    )
    assert revoked["generation"] == generation + 1

    with pytest.raises(SwarmError, match="peer_membership_generation_revoked"):
        swarm.submit_result(
            peer_id=peer_id,
            session_token=token,
            document=valid_result(work, [[1.0, 2.0]]),
        )
    assert swarm.cancel_request("generation-fenced") is True
    thread.join(timeout=1)
    assert outcome == ["request_cancelled"]


def test_browser_member_is_evidence_member_but_activation_ineligible(
    tmp_path: Any,
) -> None:
    clock = ManualClock()
    clock.value = 2_000.0
    seed = durable_seed(tmp_path, clock=clock)
    swarm = coordinator(
        clock=clock,
        wall_clock=clock,
        seed_coordinator=seed,
    )
    browser_id, _token = join(swarm)
    member = seed.member(browser_id)

    assert member["peer_class"] == "browser_http"
    assert member["activation_eligible"] is False
    with pytest.raises(
        SeedCoordinatorError,
        match="seed_member_activation_ineligible",
    ):
        seed.assignment_offer(
            node_id=browser_id,
            deployment_id="deployment-browser",
            deployment_epoch=1,
            assignment_id="activation-browser",
            assignment_digest="sha256:" + "1" * 64,
            stage_pack_digest="sha256:" + "2" * 64,
            graph_digest="sha256:" + "3" * 64,
            load_generation=1,
            peer_node_ids=[],
            placement_provenance="frozen_fixture",
        )


def test_invite_is_fragment_only_single_use_and_server_stores_no_raw_token() -> None:
    swarm = coordinator()
    invitation = swarm.create_invite(public_origin="https://swarm.example.test")

    assert invitation.url == f"https://swarm.example.test/#join/{invitation.token}"
    assert invitation.token not in repr(swarm)
    assert invitation.token not in str(swarm.debug_storage())

    grant = swarm.exchange_invite(invitation.token)
    assert grant.stage_pack["route_ready"] is False
    assert grant.peer_id
    assert grant.session_token not in str(swarm.debug_storage())

    with pytest.raises(SwarmError, match="invite_invalid_or_consumed"):
        swarm.exchange_invite(invitation.token)


def test_invite_expiry_and_origin_policy_fail_closed() -> None:
    clock = ManualClock()
    swarm = coordinator(clock=clock)
    invitation = swarm.create_invite(public_origin="http://127.0.0.1:8787")
    clock.value += 31.0
    with pytest.raises(SwarmError, match="invite_invalid_or_consumed"):
        swarm.exchange_invite(invitation.token)

    for origin in (
        "http://192.168.1.2:8787",
        "https://user@example.test",
        "https://example.test/path",
        "https://example.test?secret=x",
        "https://example.test#fragment",
        "https://example.test:99999",
        "https://example.test:invalid",
        "https://example.test\t.evil.test",
        "file:///tmp/ui",
    ):
        with pytest.raises(SwarmError, match="public_origin_invalid"):
            swarm.create_invite(public_origin=origin)


def test_invite_expiry_uses_wall_time_but_enforces_monotonic_deadline() -> None:
    clock = ManualClock()
    wall_clock = ManualClock()
    wall_clock.value = 1_750_000_000.0
    swarm = coordinator(clock=clock, wall_clock=wall_clock)

    invitation = swarm.create_invite(public_origin="https://swarm.example.test")
    assert invitation.expires_at == 1_750_000_030.0
    grant = swarm.exchange_invite(invitation.token)
    assert grant.expires_at == 1_750_000_060.0

    wall_clock.value += 10_000.0
    assert swarm.poll_work(
        peer_id=grant.peer_id,
        session_token=grant.session_token,
        timeout_seconds=0,
    ) is None
    clock.value += 61.0
    with pytest.raises(SwarmError, match="peer_expired"):
        swarm.poll_work(
            peer_id=grant.peer_id,
            session_token=grant.session_token,
            timeout_seconds=0,
        )


def test_idle_peer_expires_before_absolute_session_deadline() -> None:
    clock = ManualClock()
    swarm = coordinator(clock=clock, peer_idle_ttl_seconds=20.0)
    peer_id, token = join(swarm)

    clock.value += 21.0
    with pytest.raises(SwarmError, match="peer_expired"):
        swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=0)
    assert swarm.status()["peer_count"] == 0


def test_peer_limit_auth_revoke_leave_and_expiry() -> None:
    clock = ManualClock()
    swarm = coordinator(clock=clock, max_peers=1)
    peer_id, token = join(swarm)

    with pytest.raises(SwarmError, match="peer_capacity_exhausted"):
        join(swarm)
    with pytest.raises(SwarmError, match="peer_unauthorized"):
        swarm.poll_work(peer_id=peer_id, session_token="wrong", timeout_seconds=0)

    swarm.revoke_peer(peer_id)
    with pytest.raises(SwarmError, match="peer_revoked"):
        swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=0)

    second = coordinator(clock=clock)
    peer2, token2 = join(second)
    assert second.leave(peer_id=peer2, session_token=token2) is True
    assert second.leave(peer_id=peer2, session_token=token2) is False

    third = coordinator(clock=clock)
    peer3, token3 = join(third)
    clock.value += 61.0
    with pytest.raises(SwarmError, match="peer_expired"):
        third.poll_work(peer_id=peer3, session_token=token3, timeout_seconds=0)


def test_two_waiting_peers_are_reported_and_receive_balanced_jobs() -> None:
    swarm = coordinator()
    credentials = [join(swarm), join(swarm)]
    stop = threading.Event()
    received: list[str] = []
    errors: list[BaseException] = []

    def worker(peer_id: str, token: str) -> None:
        try:
            while not stop.is_set():
                work = swarm.poll_work(
                    peer_id=peer_id,
                    session_token=token,
                    timeout_seconds=0.05,
                )
                if work is None:
                    continue
                received.append(peer_id)
                assert start_work(swarm, peer_id, token, work) is True
                assert swarm.submit_result(
                    peer_id=peer_id,
                    session_token=token,
                    document=valid_result(work, work["hidden"]),
                ) == "accepted"
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=credential)
        for credential in credentials
    ]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 2.0
        while swarm.status()["ready_peer_count"] != 2:
            assert time.monotonic() < deadline
            time.sleep(0.005)

        swarm.dispatch(request_id="request-balanced-1", hidden=[[1.0, 2.0]], timeout_seconds=2)
        deadline = time.monotonic() + 2.0
        while swarm.status()["ready_peer_count"] != 2:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        swarm.dispatch(request_id="request-balanced-2", hidden=[[3.0, 4.0]], timeout_seconds=2)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(received) == sorted(peer_id for peer_id, _ in credentials)
    assert sorted(peer["completed_jobs"] for peer in swarm.status()["peers"]) == [1, 1]


def test_dispatch_long_poll_result_and_duplicate_idempotence() -> None:
    swarm = coordinator()
    peer_id, token = join(swarm)
    received: list[dict[str, Any]] = []

    def worker() -> None:
        work = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=2)
        assert work is not None
        received.append(work)
        assert start_work(swarm, peer_id, token, work) is True
        result = valid_result(work, [[3.0, 4.0]])
        assert swarm.submit_result(peer_id=peer_id, session_token=token, document=result) == "accepted"
        assert swarm.submit_result(peer_id=peer_id, session_token=token, document=result) == "duplicate"

    thread = threading.Thread(target=worker)
    thread.start()
    result = swarm.dispatch(
        request_id="request-1",
        hidden=[[1.0, 2.0]],
        timeout_seconds=2,
    )
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result.output == ((3.0, 4.0),)
    assert received[0]["route_ready"] is False
    assert received[0]["input_digest"] == matrix_digest([[1.0, 2.0]])
    assert "prompt" not in str(received[0]).lower()


def test_dispatch_honors_peer_exclusions_over_lifetime_balance() -> None:
    swarm = coordinator()
    selected_peer, selected_token = join(swarm)
    excluded_peer, excluded_token = join(swarm)
    with swarm._condition:  # noqa: SLF001 - scheduler regression oracle
        swarm._peers[selected_peer].completed_jobs = 10  # noqa: SLF001
    received: list[str] = []
    stop = threading.Event()

    def worker(peer_id: str, token: str) -> None:
        while not stop.is_set():
            work = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=0.05)
            if work is None:
                continue
            received.append(peer_id)
            assert start_work(swarm, peer_id, token, work) is True
            swarm.submit_result(
                peer_id=peer_id,
                session_token=token,
                document=valid_result(work, work["hidden"]),
            )

    threads = [
        threading.Thread(target=worker, args=credential)
        for credential in ((selected_peer, selected_token), (excluded_peer, excluded_token))
    ]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 2.0
        while swarm.status()["ready_peer_count"] != 2:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        result = swarm.dispatch(
            request_id="request-excluded",
            hidden=[[1.0, 2.0]],
            timeout_seconds=2,
            excluded_peer_ids={excluded_peer},
        )
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
    assert result.peer_id == selected_peer
    assert received == [selected_peer]


def test_poll_retries_same_outstanding_work_until_result() -> None:
    swarm = coordinator()
    peer_id, token = join(swarm)
    observed: list[dict[str, Any]] = []

    def worker() -> None:
        first = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=2)
        second = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=0)
        assert first == second
        assert first is not None
        observed.append(first)
        assert start_work(swarm, peer_id, token, first) is True
        swarm.submit_result(
            peer_id=peer_id,
            session_token=token,
            document=valid_result(first, [[5.0, 6.0]]),
        )

    thread = threading.Thread(target=worker)
    thread.start()
    result = swarm.dispatch(request_id="request-retry", hidden=[[0.0, 1.0]], timeout_seconds=2)
    thread.join(timeout=2)
    assert result.output == ((5.0, 6.0),)
    assert len(observed) == 1


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update(protocol="wrong"), "result_fields_or_protocol_invalid"),
        (lambda value: value.update(job_id="other"), "result_job_mismatch"),
        (lambda value: value.update(request_id="other"), "result_binding_mismatch"),
        (lambda value: value.update(pack_digest="sha256:" + "b" * 64), "result_binding_mismatch"),
        (lambda value: value.update(input_digest="sha256:" + "c" * 64), "result_binding_mismatch"),
        (lambda value: value.update(output=[[1.0]]), "result_output_invalid"),
        (lambda value: value.update(output=[[float("nan"), 1.0]]), "result_output_invalid"),
        (lambda value: value.update(output_digest="sha256:" + "d" * 64), "result_output_digest_mismatch"),
        (lambda value: value.update(route_ready=True), "result_route_ready_invalid"),
        (lambda value: value.update(extra=True), "result_fields_or_protocol_invalid"),
    ],
)
def test_result_validation_is_fail_closed(mutation: Any, code: str) -> None:
    swarm = coordinator()
    peer_id, token = join(swarm)
    outcome: list[str] = []

    def dispatch() -> None:
        with pytest.raises(SwarmError, match="dispatch_timeout"):
            swarm.dispatch(request_id="request-invalid", hidden=[[1.0, 2.0]], timeout_seconds=0.2)
        outcome.append("timed-out")

    thread = threading.Thread(target=dispatch)
    thread.start()
    work = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=1)
    assert work is not None
    assert start_work(swarm, peer_id, token, work) is True
    document = valid_result(work, [[1.0, 2.0]])
    mutation(document)
    with pytest.raises(SwarmError, match=code):
        swarm.submit_result(peer_id=peer_id, session_token=token, document=document)
    thread.join(timeout=1)
    assert outcome == ["timed-out"]


def test_conflicting_duplicate_result_rejected() -> None:
    swarm = coordinator()
    peer_id, token = join(swarm)
    accepted = threading.Event()

    def worker() -> None:
        work = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=2)
        assert work is not None
        assert start_work(swarm, peer_id, token, work) is True
        first = valid_result(work, [[1.0, 2.0]])
        assert swarm.submit_result(peer_id=peer_id, session_token=token, document=first) == "accepted"
        accepted.set()
        conflict = valid_result(work, [[2.0, 3.0]])
        with pytest.raises(SwarmError, match="result_replay_conflict"):
            swarm.submit_result(peer_id=peer_id, session_token=token, document=conflict)

    thread = threading.Thread(target=worker)
    thread.start()
    result = swarm.dispatch(request_id="request-conflict", hidden=[[1.0, 2.0]], timeout_seconds=2)
    accepted.wait(1)
    thread.join(timeout=2)
    assert result.output == ((1.0, 2.0),)


def test_cancel_and_revoke_wake_dispatcher() -> None:
    swarm = coordinator()
    peer_id, token = join(swarm)
    errors: list[str] = []

    def dispatch() -> None:
        try:
            swarm.dispatch(request_id="request-cancel", hidden=[[1.0, 2.0]], timeout_seconds=5)
        except SwarmError as exc:
            errors.append(exc.code)

    thread = threading.Thread(target=dispatch)
    thread.start()
    work = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=1)
    assert work is not None
    assert swarm.cancel_request("request-cancel") is True
    thread.join(timeout=1)
    assert errors == ["request_cancelled"]

    errors.clear()
    thread = threading.Thread(target=dispatch)
    thread.start()
    work = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=1)
    assert work is not None
    swarm.revoke_peer(peer_id)
    thread.join(timeout=1)
    assert errors == ["peer_unavailable"]


def test_cancelled_assignment_cannot_obtain_late_compute_permit() -> None:
    swarm = coordinator()
    peer_id, token = join(swarm)
    outcome: list[str] = []

    def dispatch() -> None:
        try:
            swarm.dispatch(
                request_id="request-late-start",
                hidden=[[1.0, 2.0]],
                timeout_seconds=5,
            )
        except SwarmError as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=dispatch)
    thread.start()
    work = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=1)
    assert work is not None
    assert swarm.cancel_request("request-late-start") is True
    assert start_work(swarm, peer_id, token, work) is False
    with pytest.raises(SwarmError, match="result_job_not_active"):
        swarm.submit_result(
            peer_id=peer_id,
            session_token=token,
            document=valid_result(work, [[1.0, 2.0]]),
        )
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert outcome == ["request_cancelled"]


def test_parent_cancel_event_prevents_result_acceptance_after_compute_permit() -> None:
    swarm = coordinator()
    peer_id, token = join(swarm)
    cancel_event = threading.Event()
    outcome: list[str] = []

    def dispatch() -> None:
        try:
            swarm.dispatch(
                request_id="request-result-race",
                hidden=[[1.0, 2.0]],
                timeout_seconds=5,
                cancel_event=cancel_event,
            )
        except SwarmError as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=dispatch)
    thread.start()
    work = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=1)
    assert work is not None
    assert start_work(swarm, peer_id, token, work) is True
    assert swarm.cancel_request("request-result-race", cancel_event=cancel_event) is True
    with pytest.raises(SwarmError, match="result_job_not_active"):
        swarm.submit_result(
            peer_id=peer_id,
            session_token=token,
            document=valid_result(work, [[1.0, 2.0]]),
        )
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert outcome == ["request_cancelled"]
    with swarm._condition:  # noqa: SLF001 - terminal-state regression oracle
        job = swarm._jobs[work["job_id"]]  # noqa: SLF001
        assert job.state == "cancelled"
        assert job.result is None
        assert job.result_document_digest is None


def test_status_is_sanitized_and_claim_bounded() -> None:
    swarm = coordinator()
    peer_id, token = join(swarm)
    status = swarm.status()

    assert status["protocol"] == "mycelium.interactive_status.v1"
    assert status["route_ready"] is False
    assert status["local_evidence_only"] is True
    assert status["peer_count"] == 1
    assert status["peers"][0]["peer_id"] == peer_id
    serialized = str(status)
    assert token not in serialized
    assert "secret-token" not in serialized
    assert "tensors" not in serialized
    assert "hidden" not in serialized


def test_invalid_hidden_and_queue_bound_rejected() -> None:
    swarm = coordinator()
    for hidden in ([], [[1.0]], [[True, 1.0]], [[float("inf"), 1.0]]):
        with pytest.raises(SwarmError, match="hidden_invalid"):
            swarm.dispatch(request_id="bad", hidden=hidden, timeout_seconds=0)

    errors: list[str] = []

    def blocked(index: int) -> None:
        try:
            swarm.dispatch(request_id=f"queued-{index}", hidden=[[1.0, 2.0]], timeout_seconds=0.3)
        except SwarmError as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=blocked, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 1
    while swarm.status()["pending_job_count"] < 4 and time.monotonic() < deadline:
        time.sleep(0.005)
    with pytest.raises(SwarmError, match="job_capacity_exhausted"):
        swarm.dispatch(request_id="overflow", hidden=[[1.0, 2.0]], timeout_seconds=0)
    for thread in threads:
        thread.join(timeout=1)
    assert errors.count("dispatch_timeout") == 4


def test_terminal_job_history_is_bounded() -> None:
    swarm = coordinator(max_pending_jobs=2, max_job_history=2)
    peer_id, token = join(swarm)

    for index in range(3):
        results: list[Any] = []

        def dispatch() -> None:
            results.append(
                swarm.dispatch(
                    request_id=f"bounded-{index}",
                    hidden=[[1.0, 2.0]],
                    timeout_seconds=2,
                )
            )

        thread = threading.Thread(target=dispatch)
        thread.start()
        work = swarm.poll_work(peer_id=peer_id, session_token=token, timeout_seconds=1)
        assert work is not None
        assert start_work(swarm, peer_id, token, work) is True
        swarm.submit_result(
            peer_id=peer_id,
            session_token=token,
            document=valid_result(work, [[1.0, 2.0]]),
        )
        thread.join(timeout=2)
        assert len(results) == 1

    assert len(swarm.debug_storage()["job_ids"]) == 2


def test_terminal_peer_history_is_bounded() -> None:
    swarm = coordinator(max_peers=1, max_peer_history=2)
    first_peer_id = ""
    for index in range(3):
        peer_id, token = join(swarm)
        if index == 0:
            first_peer_id = peer_id
        assert swarm.leave(peer_id=peer_id, session_token=token) is True

    peers = swarm.status()["peers"]
    assert len(peers) == 2
    assert all(peer["peer_id"] != first_peer_id for peer in peers)
