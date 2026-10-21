# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path
import socket
import sqlite3
import stat
import threading
import time
from typing import Any, Iterator, Mapping
from urllib.error import URLError as UrlError

import pytest

from mycelium_a13_macos.authority import DemoAuthority
from mycelium_a13_macos.managed_control import (
    CONTROL_COMMAND_PROTOCOL,
    ManagedControlClient,
    ManagedControlError,
    ManagedControlMailbox,
    LocalTLSManagedControlServer,
    OWNER_COMMAND_PROTOCOL,
    sign_managed_statement,
)
from mycelium_a13_macos.peer import RemoteMacMember
from mycelium_a13_macos.tls import pinned_seed_client, pinned_seed_client_from_invite
from mycelium_node.identity import load_node_signer
from mycelium_qualification.evidence import canonical_json_bytes


CONSENT = {
    "cooperative_peer_visibility_acknowledged": True,
    "resource_use_acknowledged": True,
    "contribution_enabled": True,
}
QUOTA = {
    "storage_bytes": 2_000_000_000,
    "network_bytes_per_day": 5_000_000_000,
    "background_allowed": False,
}
RUN_ID = "a13-managed-run-1"
DEPLOYMENT_ID = "a13-managed-deployment-1"


@contextmanager
def _world(
    tmp_path: Path, *, control_clock=time.time,
    mailbox_options: Mapping[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    authority = DemoAuthority(tmp_path / "owner")
    authority.start()
    authority.create_swarm(CONSENT)
    assert authority.coordinator is not None
    assert authority.seed_server is not None
    ca = authority.seed_server.material.ca_certificate

    def seed_client_factory(*, bundle=None, membership=None, now, timeout):
        if bundle is not None:
            return pinned_seed_client_from_invite(
                bundle,
                now=now,
                ca_certificate=ca,
                timeout=timeout,
            )
        return pinned_seed_client(
            seed_url=membership["seed_url"],
            swarm_id=membership["swarm_id"],
            seed_key_digest=membership["seed_key_digest"],
            seed_key_records=list(membership["seed_key_records"]),
            ca_certificate=ca,
            timeout=timeout,
        )

    target_root = tmp_path / "target"
    target = RemoteMacMember(
        target_root,
        _test_seed_client_factory=seed_client_factory,
    )
    target.start()
    request = target.pair_request(CONSENT, ttl_seconds=300)
    handoff = authority.approve_pairing(request, ttl_seconds=120, quota=QUOTA)
    target.accept_handoff(handoff)
    member = authority.coordinator.member(request["node_id"])
    owner_signer = authority.coordinator.signer
    target_signer = load_node_signer(target_root / "identity" / "node.key")
    database = tmp_path / "control" / "mailbox.sqlite3"
    mailbox = ManagedControlMailbox(
        database, coordinator=authority.coordinator, clock=control_clock,
        **({} if mailbox_options is None else dict(mailbox_options)),
    )
    material = authority.seed_server.material
    server = LocalTLSManagedControlServer(
        mailbox,
        certificate=material.server_certificate,
        private_key=material.server_private_key,
    ).start()
    owner_client = ManagedControlClient.for_local_tls(
        base_url=server.base_url,
        signer=owner_signer,
        trusted_owner_key_record=owner_signer.public_key_record(),
        swarm_id=authority.coordinator.swarm_id,
        owner_node_id=authority.coordinator.seed_node_id,
        clock=control_clock,
        ca_certificate=material.ca_certificate,
    )
    target_client = ManagedControlClient.for_local_tls(
        base_url=server.base_url,
        signer=target_signer,
        trusted_owner_key_record=owner_signer.public_key_record(),
        swarm_id=authority.coordinator.swarm_id,
        owner_node_id=authority.coordinator.seed_node_id,
        clock=control_clock,
        ca_certificate=material.ca_certificate,
    )
    try:
        yield {
            "authority": authority,
            "target": target,
            "member": member,
            "mailbox": mailbox,
            "server": server,
            "owner_client": owner_client,
            "target_client": target_client,
            "owner_signer": owner_signer,
            "target_signer": target_signer,
            "database": database,
        }
    finally:
        server.close()
        target.stop()
        authority.stop()


def _command(command_id: str, operation: str = "hello", payload=None) -> dict[str, Any]:
    return {
        "protocol": CONTROL_COMMAND_PROTOCOL,
        "command_id": command_id,
        "run_id": RUN_ID,
        "deployment_id": DEPLOYMENT_ID,
        "command": operation,
        "payload": {} if payload is None else payload,
    }


def _allowed_payload(operation: str) -> dict[str, Any]:
    if operation == "configure":
        return {
            "assignment_file": "assignment.json",
            "manifest_file": "manifest.json",
            "stage_pack_file": "stage-pack.json",
            "graph": {},
            "device_states": {},
            "load_generation": 1,
        }
    if operation == "start":
        return {
            "peer": {
                "node_id": "peer-1",
                "endpoint_id": "endpoint-1",
                "endpoint_addr": "iroh://endpoint-1",
                "generation": 1,
            },
            "local_generation": 1,
        }
    if operation == "cancel":
        return {"request_id": "request-1"}
    return {}


def _native_response(command_id: str, node_id: str, *, ok: bool = True) -> dict[str, Any]:
    common = {
        "protocol": CONTROL_COMMAND_PROTOCOL,
        "command_id": command_id,
        "node_id": node_id,
        "ok": ok,
        "route_ready": False,
    }
    if ok:
        return {**common, "result": {"state": "NEW"}}
    return {**common, "error": {"code": "invalid_state_for_start"}}


def _enqueue(world: dict[str, Any], command_id: str, *, sequence: int = 1, operation="hello", payload=None):
    return world["owner_client"].enqueue(
        target=world["member"],
        native_command=_command(command_id, operation, payload),
        sequence=sequence,
        nonce=f"owner-nonce-{sequence}",
        request_id=f"owner-request-{sequence}",
    )


def test_real_tls_signature_sqlite_enqueue_poll_ack_and_private_restart(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        enqueued = _enqueue(world, "hello-1")
        assert enqueued["state"] == "pending"
        assert enqueued["command_id"] == "hello-1"

        delivery = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=1,
            nonce="target-nonce-1",
            request_id="target-poll-1",
        )
        assert delivery["delivery_kind"] == "command"
        assert delivery["command_envelope"]["statement"]["native_command"] == _command(
            "hello-1"
        )
        assert isinstance(delivery["claim_token"], str)

        terminal = world["target_client"].ack(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            command_id="hello-1",
            claim_token=delivery["claim_token"],
            disposition="completed",
            native_response=_native_response("hello-1", world["member"]["node_id"]),
            sequence=2,
            nonce="target-nonce-2",
            request_id="target-ack-2",
        )
        assert terminal["state"] == "completed"
        assert terminal["command_id"] == "hello-1"
        assert terminal["native_response_digest"].startswith("sha256:")

        # Durable restart reads the same terminal receipt and does not redeliver.
        restarted = ManagedControlMailbox(
            world["database"], coordinator=world["authority"].coordinator
        )
        assert restarted.command_receipt("hello-1")["state"] == "completed"
        empty = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=3,
            nonce="target-nonce-3",
            request_id="target-poll-3",
        )
        assert empty["delivery_kind"] == "empty"

        assert stat.S_IMODE(world["database"].lstat().st_mode) == 0o600
        assert stat.S_IMODE(world["database"].parent.lstat().st_mode) == 0o700
        with sqlite3.connect(world["database"]) as connection:
            row = connection.execute(
                "SELECT state, native_response_json FROM managed_commands WHERE command_id = ?",
                ("hello-1",),
            ).fetchone()
        assert row is not None and row[0] == "completed" and row[1] is not None


def test_allowlist_exact_native_envelope_and_no_generic_execution_inputs(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        for index, operation in enumerate(
            ("hello", "configure", "start", "health", "snapshot", "cancel", "stop"),
            1,
        ):
            payload = _allowed_payload(operation)
            result = _enqueue(
                world,
                f"allowed-{operation}",
                sequence=index,
                operation=operation,
                payload=payload,
            )
            assert result["state"] == "pending"

        for sequence, operation, payload in (
            (8, "infer_start", {}),
            (8, "exec", {}),
            (8, "configure", {"argv": ["/bin/sh", "-c", "whoami"]}),
            (8, "configure", {"python": "import os"}),
            (8, "configure", {"script_path": "/tmp/run.py"}),
        ):
            with pytest.raises(ManagedControlError) as rejected:
                _enqueue(
                    world,
                    f"rejected-{operation}-{len(canonical_json_bytes(payload))}",
                    sequence=sequence,
                    operation=operation,
                    payload=payload,
                )
            expected = {"managed_control_native_command_invalid"} if operation == "infer_start" else {
                "managed_control_operation_unsupported", "managed_control_execution_input_forbidden"}
            # infer_start is now supported, but an empty payload remains invalid.
            assert rejected.value.code in expected

        malformed = _command("outer-extra")
        malformed["argv"] = ["anything"]
        now = time.time()
        statement = {
            "protocol": OWNER_COMMAND_PROTOCOL,
            "request_id": "owner-request-outer-extra",
            "swarm_id": world["authority"].coordinator.swarm_id,
            "owner_node_id": world["authority"].coordinator.seed_node_id,
            "target_node_id": world["member"]["node_id"],
            "target_endpoint_id": world["member"]["endpoint_id"],
            "target_key_digest": world["member"]["verification_key_digest"],
            "membership_generation": world["member"]["generation"],
            "member_incarnation": world["member"]["incarnation"],
            "run_id": RUN_ID,
            "deployment_id": DEPLOYMENT_ID,
            "sequence": 8,
            "nonce": "owner-nonce-8",
            "issued_at": now,
            "expires_at": now + 60,
            "native_command": malformed,
        }
        envelope = sign_managed_statement(world["owner_signer"], statement)
        with pytest.raises(ManagedControlError, match="managed_control_native_command_invalid"):
            world["mailbox"].enqueue(envelope)


def test_tamper_freshness_sequence_nonce_and_conflicting_retry_fail_closed(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        first = _enqueue(world, "hello-replay")
        assert first == _enqueue(world, "hello-replay")

        with pytest.raises(ManagedControlError, match="managed_control_conflicting_retry"):
            world["owner_client"].enqueue(
                target=world["member"],
                native_command=_command("hello-replay", "health"),
                sequence=2,
                nonce="owner-nonce-2",
                request_id="owner-request-2",
            )

        with pytest.raises(ManagedControlError, match="managed_control_sequence_replayed"):
            world["owner_client"].enqueue(
                target=world["member"],
                native_command=_command("new-command"),
                sequence=1,
                nonce="owner-new-nonce",
                request_id="owner-new-request",
            )
        with pytest.raises(ManagedControlError, match="managed_control_nonce_replayed"):
            world["owner_client"].enqueue(
                target=world["member"],
                native_command=_command("new-command"),
                sequence=2,
                nonce="owner-nonce-1",
                request_id="owner-new-request-2",
            )

        now = time.time()
        stale_statement = world["owner_client"].owner_command_statement(
            target=world["member"],
            native_command=_command("expired-command"),
            sequence=2,
            nonce="owner-nonce-expired",
            request_id="owner-expired-request",
            issued_at=now,
            expires_at=now + 60,
        )
        stale_statement["issued_at"] = now - 120
        stale_statement["expires_at"] = now - 60
        with pytest.raises(ManagedControlError, match="managed_control_message_expired"):
            world["mailbox"].enqueue(
                sign_managed_statement(world["owner_signer"], stale_statement)
            )

        tampered = sign_managed_statement(world["owner_signer"], {
            **world["owner_client"].owner_command_statement(
                target=world["member"],
                native_command=_command("tampered-command"),
                sequence=2,
                nonce="owner-nonce-tamper",
                request_id="owner-tamper-request",
                issued_at=now,
                expires_at=now + 60,
            )
        })
        tampered["statement"] = copy.deepcopy(tampered["statement"])
        tampered["statement"]["native_command"]["command"] = "stop"
        with pytest.raises(ManagedControlError, match="managed_control_signature_invalid"):
            world["mailbox"].enqueue(tampered)


def test_expired_pending_command_is_terminally_fenced_before_delivery(tmp_path: Path) -> None:
    now = [time.time()]
    with _world(tmp_path, control_clock=lambda: now[0]) as world:
        world["owner_client"].enqueue(
            target=world["member"],
            native_command=_command("expire-before-poll"),
            sequence=1,
            nonce="owner-expire-1",
            request_id="owner-expire-request-1",
            ttl_seconds=30.0,
        )
        now[0] += 31.0
        delivery = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=1,
            nonce="target-after-expiry-1",
            request_id="target-after-expiry-request-1",
        )
        assert delivery["delivery_kind"] == "empty"
        receipt = world["mailbox"].command_receipt("expire-before-poll")
        assert receipt["state"] == "expired"
        assert receipt["native_response_digest"] is None


def test_run_and_deployment_bindings_prevent_cross_delivery_and_cross_ack(
    tmp_path: Path,
) -> None:
    with _world(tmp_path) as world:
        _enqueue(world, "bound-command")
        wrong_run = world["target_client"].poll(
            target=world["member"],
            run_id="other-run",
            deployment_id=DEPLOYMENT_ID,
            sequence=1,
            nonce="target-wrong-run-1",
            request_id="target-wrong-run-request-1",
        )
        assert wrong_run["delivery_kind"] == "empty"
        claimed = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=2,
            nonce="target-right-run-2",
            request_id="target-right-run-request-2",
        )
        assert claimed["delivery_kind"] == "command"
        with pytest.raises(
            ManagedControlError, match="managed_control_command_binding_mismatch"
        ):
            world["target_client"].ack(
                target=world["member"],
                run_id=RUN_ID,
                deployment_id="other-deployment",
                command_id="bound-command",
                claim_token=claimed["claim_token"],
                disposition="completed",
                native_response=_native_response(
                    "bound-command", world["member"]["node_id"]
                ),
                sequence=3,
                nonce="target-wrong-ack-3",
                request_id="target-wrong-ack-request-3",
            )
        terminal = world["target_client"].ack(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            command_id="bound-command",
            claim_token=claimed["claim_token"],
            disposition="completed",
            native_response=_native_response(
                "bound-command", world["member"]["node_id"]
            ),
            sequence=3,
            nonce="target-right-ack-3",
            request_id="target-right-ack-request-3",
        )
        assert terminal["state"] == "completed"


def test_member_is_revalidated_before_enqueue_and_delivery(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        _enqueue(world, "pending-before-fence")
        world["authority"].coordinator.advance_member_generation(
            node_id=world["member"]["node_id"],
            expected_generation=world["member"]["generation"],
            lifecycle_state="STOPPED",
        )
        with pytest.raises(ManagedControlError, match="managed_control_member_stale"):
            world["target_client"].poll(
                target=world["member"],
                run_id=RUN_ID,
                deployment_id=DEPLOYMENT_ID,
                sequence=1,
                nonce="target-stale-1",
                request_id="target-stale-poll",
            )
        with pytest.raises(ManagedControlError, match="managed_control_member_stale"):
            world["owner_client"].enqueue(
                target=world["member"],
                native_command=_command("enqueue-after-fence"),
                sequence=2,
                nonce="owner-after-fence",
                request_id="owner-after-fence",
            )


def test_interrupted_claim_requires_reconciliation_and_is_never_blindly_replayed(
    tmp_path: Path,
) -> None:
    with _world(tmp_path) as world:
        _enqueue(
            world,
            "start-crash",
            operation="start",
            payload=_allowed_payload("start"),
        )
        claimed = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=1,
            nonce="target-claim-1",
            request_id="target-claim-request-1",
        )
        assert claimed["delivery_kind"] == "command"

        # Simulate mailbox restart after the target could have applied the command.
        world["mailbox"].close()
        replacement = ManagedControlMailbox(
            world["database"], coordinator=world["authority"].coordinator
        )
        world["server"].replace_mailbox_for_test(replacement)
        world["mailbox"] = replacement

        reconcile = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=2,
            nonce="target-reconcile-2",
            request_id="target-reconcile-request-2",
        )
        assert reconcile["delivery_kind"] == "reconciliation_required"
        assert reconcile["command_id"] == "start-crash"
        assert reconcile["claim_token"] == claimed["claim_token"]
        assert reconcile["command_envelope"] is None

        reconciled_unknown = world["target_client"].ack(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            command_id="start-crash",
            claim_token=claimed["claim_token"],
            disposition="reconciliation_required",
            native_response=None,
            sequence=3,
            nonce="target-reconcile-ack-3",
            request_id="target-reconcile-ack-request-3",
        )
        assert reconciled_unknown["state"] == "reconciliation_required"
        receipt = world["mailbox"].command_receipt("start-crash")
        assert receipt["state"] == "reconciliation_required"
        assert receipt["native_response_digest"] is None
        with sqlite3.connect(world["database"]) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT state, terminal_receipt_json, native_response_digest, "
                "native_response_json FROM managed_commands WHERE command_id = ?",
                ("start-crash",),
            ).fetchone()
        assert row["state"] == "reconciliation_required"
        assert row["terminal_receipt_json"] is not None
        assert row["native_response_digest"] is None
        assert row["native_response_json"] is None

        # The owner's uncertainty is now a durable terminal outcome for this
        # command, not an eternal poll demand that bricks the peer.
        after_reconcile = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=4,
            nonce="target-after-reconcile-4",
            request_id="target-after-reconcile-request-4",
        )
        assert after_reconcile["delivery_kind"] == "empty"
        assert after_reconcile["command_id"] is None
        assert after_reconcile["claim_token"] is None
        assert after_reconcile["command_envelope"] is None

        with pytest.raises(ManagedControlError, match="managed_control_conflicting_retry"):
            world["target_client"].ack(
                target=world["member"],
                run_id=RUN_ID,
                deployment_id=DEPLOYMENT_ID,
                command_id="start-crash",
                claim_token=claimed["claim_token"],
                disposition="completed",
                native_response=_native_response(
                    "start-crash", world["member"]["node_id"]
                ),
                sequence=5,
                nonce="target-reconcile-ack-5",
                request_id="target-reconcile-ack-request-5",
            )


def test_native_receipt_can_resolve_reconciliation_demand_before_unknown_ack(
    tmp_path: Path,
) -> None:
    with _world(tmp_path) as world:
        _enqueue(
            world,
            "start-recovered",
            operation="start",
            payload=_allowed_payload("start"),
        )
        claimed = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=1,
            nonce="target-claim-recovered-1",
            request_id="target-claim-recovered-request-1",
        )
        assert claimed["delivery_kind"] == "command"

        # The peer no longer lists the claim as active, so the owner demands
        # reconciliation exactly once.  If the peer actually retained the native
        # result, a real terminal native receipt may still resolve the command.
        reconcile = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=2,
            nonce="target-demand-recovered-2",
            request_id="target-demand-recovered-request-2",
        )
        assert reconcile["delivery_kind"] == "reconciliation_required"
        assert reconcile["command_id"] == "start-recovered"
        assert reconcile["claim_token"] == claimed["claim_token"]
        assert reconcile["command_envelope"] is None

        terminal = world["target_client"].ack(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            command_id="start-recovered",
            claim_token=claimed["claim_token"],
            disposition="completed",
            native_response=_native_response(
                "start-recovered", world["member"]["node_id"]
            ),
            sequence=3,
            nonce="target-terminal-recovered-3",
            request_id="target-terminal-recovered-request-3",
        )
        assert terminal["state"] == "completed"
        assert world["mailbox"].command_receipt("start-recovered")["state"] == "completed"

        empty = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=4,
            nonce="target-after-terminal-4",
            request_id="target-after-terminal-request-4",
        )
        assert empty["delivery_kind"] == "empty"


def test_unreachable_route_is_attributable_and_failure_diagnostic_is_bounded(
    tmp_path: Path,
) -> None:
    """A collapsed generic code must still name the failing route privately.

    `managed_control_unreachable` alone cost a debugging cycle: the operator
    cannot tell which of several requests during one start failed.  The route is
    now part of the bounded error identity, and the app records that identity in
    a private allowlisted diagnostic instead of only the public generic code.
    """
    from mycelium_a13_macos.app import bounded_action_failure, public_failure_code

    with _world(tmp_path) as world:
        dead = ManagedControlClient(
            base_url="https://127.0.0.1:9",
            signer=world["target_signer"],
            trusted_owner_key_record=world["owner_signer"].public_key_record(),
            swarm_id=world["authority"].coordinator.swarm_id,
            owner_node_id=world["authority"].coordinator.seed_node_id,
            timeout=1.0,
        )
        failure = None
        try:
            dead.poll(
                target=world["member"],
                run_id=RUN_ID,
                deployment_id=DEPLOYMENT_ID,
                sequence=1,
                nonce="dead-nonce-1",
                request_id="dead-request-1",
            )
        except ManagedControlError as caught:
            failure = caught
        assert failure is not None, "expected an unreachable managed-control route"
        assert failure.code == "managed_control_unreachable"
        assert failure.request_path == "/managed-control/poll"
        # Arbitrary OS message text is never propagated as an identity.
        assert failure.cause_code is None

        record = bounded_action_failure("/api/control/start", failure, public_failure_code(failure))
        assert record["protocol"] == "mycelium.a13.action_failure.v1"
        assert record["path"] == "/api/control/start"
        # The library-owned code is already specific; the diagnostic adds the
        # route, which is the fact the public body still cannot carry.
        assert record["public_code"] == "managed_control_unreachable"
        assert record["error_code"] == "managed_control_unreachable"
        assert record["request_path"] == "/managed-control/poll"
        assert record["exception_type"] == "ManagedControlError"
        # Bounded identity only: never exception text or credentials.
        assert set(record) <= {
            "protocol", "path", "public_code", "exception_type",
            "exception_module", "error_code", "status", "request_path",
        }


@pytest.mark.parametrize(
    "error,expected",
    [
        (OSError("relay_dns_nonpublic"), "relay_dns_nonpublic"),
        (UrlError(socket.gaierror(8, "nodename nor servname provided")), "dns_resolution_failed"),
        (OSError("relay_connect_unavailable"), "relay_connect_unavailable"),
        (OSError("[Errno 61] Connection refused"), None),
        (OSError("something arbitrary with spaces"), None),
        (TimeoutError("relay_connect_timeout"), "relay_connect_timeout"),
    ],
)
def test_unreachable_cause_is_bounded_to_allowlisted_identities(error, expected) -> None:
    """A fail-closed library guard must not be masked by the generic code.

    `relay_dns_nonpublic` and a transient network drop both surfaced as
    `managed_control_unreachable`; only a coded token (or a generic DNS/timeout
    classification) is retained, and raw OS text is discarded.
    """
    from mycelium_a13_macos.managed_control import _bounded_unreachable_cause

    assert _bounded_unreachable_cause(error) == expected


def test_http_response_signature_and_no_redirect_client_policy(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        assert world["owner_client"].redirects_allowed is False
        assert world["target_client"].redirects_allowed is False
        with pytest.raises(ManagedControlError, match="managed_control_signature_invalid"):
            world["owner_client"].poll(
                target=world["member"],
                run_id=RUN_ID,
                deployment_id=DEPLOYMENT_ID,
                sequence=50,
                nonce="wrong-target-signer-50",
                request_id="wrong-target-signer-request-50",
            )
        _enqueue(world, "signed-delivery")
        result = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=1,
            nonce="target-signed-1",
            request_id="target-signed-request-1",
        )
        assert result["delivery_kind"] == "command"
        bad_owner = load_node_signer(world["target"].state_root / "identity" / "node.key")
        wrong_pin_client = ManagedControlClient.for_local_tls(
            base_url=world["server"].base_url,
            signer=world["target_signer"],
            trusted_owner_key_record=bad_owner.public_key_record(),
            swarm_id=world["authority"].coordinator.swarm_id,
            owner_node_id=world["authority"].coordinator.seed_node_id,
            ca_certificate=world["authority"].seed_server.material.ca_certificate,
        )
        with pytest.raises(ManagedControlError, match="managed_control_response_signature_invalid"):
            wrong_pin_client.poll(
                target=world["member"],
                run_id=RUN_ID,
                deployment_id=DEPLOYMENT_ID,
                sequence=2,
                nonce="target-wrong-pin-2",
                request_id="target-wrong-pin-request-2",
            )


# ---------------------------------------------------------------------------
# Bounded long poll: the delivery-path fix for the relayed peer's claim latency.
#
# Measured (round 52 §13/§17): the target's concurrent pump performs exactly one
# synchronous relay HTTP operation per `step()` and then sleeps 50 ms, so a
# cleanup command enqueued just after a poll departed waits a full cycle to be
# claimed -- ~1.03 s on the relayed peer against ~0.119 s for the same-machine
# node.  Claim + transport drain + receipt seal then exceeds the route's
# immutable 2.0 s cancellation window, so cleanup can never be proven in time.
# ---------------------------------------------------------------------------


def _concurrent_poll(
    world: dict[str, Any],
    *,
    sequence: int = 1,
    nonce: str = "target-long-poll-nonce-1",
    request_id: str = "target-long-poll-request-1",
    active_claims: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One concurrent (v2) target poll, exactly as the target pump issues it.

    The client reuses the exact signed bytes for an identical retry, so calling
    this twice with the same identifiers replays one signed request -- the same
    thing `TargetControlPump._replay` does for a durable outbound.

    `active_claims` is the pump's own list of claims it has not terminally
    acknowledged, so an empty map means the peer is idle and owes nothing.
    """

    return world["target_client"].poll(
        target=world["member"],
        run_id=RUN_ID,
        deployment_id=DEPLOYMENT_ID,
        sequence=sequence,
        nonce=nonce,
        request_id=request_id,
        active_claims={} if active_claims is None else dict(active_claims),
    )


def test_empty_concurrent_poll_holds_open_and_delivers_when_a_command_is_enqueued(
    tmp_path: Path,
) -> None:
    """An empty poll must wait for a command instead of returning at the pump cycle.

    RED before the fix: the poll returns `delivery_kind == 'empty'` immediately,
    so the peer learns about a cleanup command only on its next cycle -- the
    measured `relay RTT + 50 ms` claim latency this change removes.
    """

    with _world(tmp_path, mailbox_options={"long_poll_hold": 2.0}) as world:
        outcome: dict[str, Any] = {}

        def held() -> None:
            outcome["delivery"] = _concurrent_poll(world)

        parked = threading.Thread(target=held, name="held-control-poll", daemon=True)
        parked.start()
        parked.join(0.5)
        assert parked.is_alive(), (
            "the empty poll returned immediately instead of holding open for a command"
        )

        started = time.monotonic()
        _enqueue(world, "held-poll-delivery", operation="cancel", payload=_allowed_payload("cancel"))
        parked.join(10.0)
        assert not parked.is_alive(), "the held poll never returned after a command was enqueued"
        claim_seconds = time.monotonic() - started

        delivery = outcome["delivery"]
        assert delivery["delivery_kind"] == "command"
        assert delivery["command_id"] == "held-poll-delivery"
        assert claim_seconds < 0.5, (
            f"the held poll did not deliver promptly ({claim_seconds:.3f}s); a full poll cycle "
            "is what this hold exists to remove"
        )


def test_a_held_poll_never_serialises_the_owners_own_enqueue(tmp_path: Path) -> None:
    """Trap A: the hold must sit between attempts, not inside the lock or transaction.

    A long poll that holds the process-wide mailbox lock (or one `BEGIN
    IMMEDIATE` transaction) across the wait would serialise the owner's own
    `enqueue` behind it -- and the enqueue this path exists to deliver is the
    cancellation itself, so that would be strictly worse than the short poll.
    """

    with _world(tmp_path, mailbox_options={"long_poll_hold": 2.0}) as world:
        outcome: dict[str, Any] = {}

        def held() -> None:
            outcome["delivery"] = _concurrent_poll(world)

        parked = threading.Thread(target=held, name="held-control-poll", daemon=True)
        parked.start()
        parked.join(0.5)
        assert parked.is_alive(), "no hold was in progress, so this trap could not be exercised"

        started = time.monotonic()
        _enqueue(world, "enqueue-not-serialised", operation="cancel", payload=_allowed_payload("cancel"))
        enqueue_seconds = time.monotonic() - started
        assert enqueue_seconds < 0.5, (
            f"the owner's enqueue waited {enqueue_seconds:.3f}s behind a held poll"
        )

        parked.join(10.0)
        assert not parked.is_alive()
        assert outcome["delivery"]["command_id"] == "enqueue-not-serialised"


def test_the_hold_is_bounded_and_returns_the_empty_receipt_at_its_deadline(
    tmp_path: Path,
) -> None:
    """The hold must be spent when nothing arrives, and must not exceed its bound."""

    with _world(tmp_path, mailbox_options={"long_poll_hold": 0.4}) as world:
        started = time.monotonic()
        delivery = _concurrent_poll(world)
        elapsed = time.monotonic() - started
        assert delivery["delivery_kind"] == "empty"
        assert elapsed >= 0.4, f"the hold was not spent ({elapsed:.3f}s)"
        assert elapsed < 3.0, f"the hold is not bounded ({elapsed:.3f}s)"


def test_an_empty_poll_receipt_is_never_replayed_from_the_response_cache(
    tmp_path: Path,
) -> None:
    """Trap B: `_cached_or_guard` is keyed on the request digest.

    Every re-attempt inside the hold replays one signed request, so without this
    the second attempt would return the recorded *empty* receipt and the entire
    hold would be a silent no-op -- the difference between a working long poll
    and nothing at all.
    """

    with _world(tmp_path, mailbox_options={"long_poll_hold": 0.0}) as world:
        empty = _concurrent_poll(world)
        assert empty["delivery_kind"] == "empty"
        _enqueue(world, "after-empty-receipt")
        delivered = _concurrent_poll(world)
        assert delivered["delivery_kind"] == "command", (
            "the recorded empty receipt was replayed instead of a fresh claim"
        )
        assert delivered["command_id"] == "after-empty-receipt"


def test_a_delivered_poll_receipt_is_still_replayed_verbatim(tmp_path: Path) -> None:
    """The empty-receipt bypass must not disable replay for a delivered claim.

    Recomputing a replayed poll that already delivered a command would turn the
    same signed retry into a false reconciliation demand: the envelope's
    `active_claims` cannot list the claim it is being told about.
    """

    from mycelium_a13_macos.managed_control import _document_digest

    with _world(tmp_path, mailbox_options={"long_poll_hold": 0.0}) as world:
        _enqueue(world, "cached-delivery")
        first = _concurrent_poll(world)
        assert first["delivery_kind"] == "command"
        second = _concurrent_poll(world)
        assert second["delivery_kind"] == "command"
        assert _document_digest(second) == _document_digest(first)
        assert second["claim_token"] == first["claim_token"]


def test_a_legacy_serial_poll_keeps_its_immediate_empty_receipt(tmp_path: Path) -> None:
    """Scope: the hold belongs to the concurrent pump the demo actually runs.

    The legacy serial pump has no measured delivery defect and its own cadence,
    so its poll keeps returning immediately -- the change cannot slow it down or
    alter its ordering.
    """

    with _world(tmp_path, mailbox_options={"long_poll_hold": 2.0}) as world:
        started = time.monotonic()
        delivery = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=9,
            nonce="target-serial-nonce-9",
            request_id="target-serial-request-9",
        )
        elapsed = time.monotonic() - started
        assert delivery["delivery_kind"] == "empty"
        assert elapsed < 1.0, f"a legacy serial poll was held for {elapsed:.3f}s"


def test_a_poll_that_owes_an_acknowledgement_is_never_held_open(tmp_path: Path) -> None:
    """Trap C: never park a peer whose outstanding ack still has a live deadline.

    `active_claims` lists every claim the pump has not terminally acked, so a
    non-empty map means the peer still has native work to report.  Parking that
    poll holds the pump behind the hold and delays exactly that acknowledgement --
    measured by `tests/a13_macos/test_owner_concurrency.py`, whose owner deadline
    of 1.0 s loses to an ungated 1.5 s park, and by the round-50/52 mailbox ledger,
    where a health claim was still outstanding when the cleanup cancel arrived.

    wp5-cleanup-levers keeps this guard exactly where it protects a *live*
    deadline (the claim's own `command_expires_at`, which the owner's waiter uses
    as its deadline -- `control_session._send`: `ttl_seconds=min(60.0,
    _remaining(deadline))`), and narrows it only for a claim whose deadline has
    already passed; see
    `test_a_poll_that_owes_an_expired_acknowledgement_is_held_open`.
    """

    with _world(tmp_path, mailbox_options={"long_poll_hold": 2.0}) as world:
        _enqueue(world, "outstanding-claim")
        delivery = _concurrent_poll(world)
        assert delivery["delivery_kind"] == "command"
        started = time.monotonic()
        follow_up = _concurrent_poll(
            world,
            sequence=2,
            nonce="target-long-poll-nonce-2",
            request_id="target-long-poll-request-2",
            active_claims={delivery["command_id"]: delivery["claim_token"]},
        )
        elapsed = time.monotonic() - started
        assert follow_up["delivery_kind"] == "empty"
        assert elapsed < 1.0, (
            f"a poll that still owed a live-deadline acknowledgement was parked for {elapsed:.3f}s"
        )
        # The claim really did still have a live owner deadline when it was
        # refused a hold: the default command TTL is 60 s.
        assert world["mailbox"]._outstanding_claim_deadline_is_live(
            {delivery["command_id"]: delivery["claim_token"]}
        )


def test_a_poll_that_owes_an_expired_acknowledgement_is_held_open(tmp_path: Path) -> None:
    """wp5-cleanup-levers: the hold reaches the case the ledger actually records.

    The mailbox ledger records a health claim still outstanding when the cleanup
    cancel arrived, i.e. the peer was "usually *not* idle" - so the shipped gate
    made the hold inapplicable to the measured case and the relayed peer's
    ~1.03 s claim latency survived (round 52 section 17: removing that wait
    leaves ~return transit, ~0.4-0.5 s).

    A claim whose own `command_expires_at` has passed cannot be observed by any
    live waiter - that instant IS the waiter's deadline - so parking a poll that
    owes only such acknowledgements cannot delay an observed ack past a deadline,
    and the cancel enqueued during the hold is claimed in one re-attempt instead
    of after a whole pump cycle.

    RED before the widening: a non-empty `active_claims` map refused the hold
    unconditionally, so this poll returned immediately and the assertion below
    ("the empty poll was not held open") failed.
    """

    # Exercise expiry after a successful claim, independently of TLS/setup
    # scheduling. The hold and wake assertions below still use real monotonic time.
    now = [time.time()]
    with _world(tmp_path, control_clock=lambda: now[0],
                mailbox_options={"long_poll_hold": 2.0}) as world:
        enqueued = world["owner_client"].enqueue(
            target=world["member"],
            native_command=_command("expired-health-probe", operation="health"),
            sequence=1,
            nonce="owner-nonce-1",
            request_id="owner-request-1",
            ttl_seconds=0.2,
        )
        assert enqueued["state"] == "pending"
        delivery = _concurrent_poll(world)
        assert delivery["delivery_kind"] == "command"
        assert delivery["command_id"] == "expired-health-probe"
        active = {delivery["command_id"]: delivery["claim_token"]}
        assert world["mailbox"]._outstanding_claim_deadline_is_live(active)
        now[0] += 0.25  # the owner's own 0.2 s deadline passes after claim
        assert not world["mailbox"]._outstanding_claim_deadline_is_live(active)

        outcome: dict[str, Any] = {}

        def held() -> None:
            outcome["delivery"] = _concurrent_poll(
                world,
                sequence=2,
                nonce="target-long-poll-nonce-2",
                request_id="target-long-poll-request-2",
                active_claims=active,
            )

        parked = threading.Thread(target=held, name="held-expired-ack-poll", daemon=True)
        parked.start()
        parked.join(0.5)
        assert parked.is_alive(), (
            "a poll whose only outstanding acknowledgement had already expired was "
            "not held open; the measured relayed claim is not covered"
        )

        started = time.monotonic()
        _enqueue(
            world,
            "cancel-during-expired-ack-hold",
            sequence=3,
            operation="cancel",
            payload=_allowed_payload("cancel"),
        )
        parked.join(10.0)
        assert not parked.is_alive(), "the held poll never returned after a command was enqueued"
        claim_seconds = time.monotonic() - started

        assert outcome["delivery"]["delivery_kind"] == "command"
        assert outcome["delivery"]["command_id"] == "cancel-during-expired-ack-hold"
        assert claim_seconds < 0.5, (
            f"the held poll did not deliver promptly ({claim_seconds:.3f}s); the cycle the "
            "hold exists to remove is the measured claim latency"
        )


def test_the_hold_is_refused_while_any_outstanding_claim_has_a_live_deadline(
    tmp_path: Path,
) -> None:
    """The widening is bounded by the live-deadline rule, not by claim count.

    One expired claim must not license a park while another claim the peer owes
    still has a live owner deadline: the pump performs one HTTP operation per
    ``step()``, so a held poll delays *every* outstanding acknowledgement, and a
    live one is exactly the case ``test_owner_concurrency`` measured at 1.0 s
    against an ungated 1.5 s park.
    """

    now = [time.time()]
    with _world(tmp_path, control_clock=lambda: now[0],
                mailbox_options={"long_poll_hold": 2.0}) as world:
        world["owner_client"].enqueue(
            target=world["member"],
            native_command=_command("short-probe", operation="health"),
            sequence=1,
            nonce="owner-nonce-1",
            request_id="owner-request-1",
            ttl_seconds=0.2,
        )
        _enqueue(world, "long-ledger-claim", sequence=2)
        first = _concurrent_poll(world)
        assert first["delivery_kind"] == "command"
        second = _concurrent_poll(
            world, sequence=2, nonce="target-long-poll-nonce-2",
            request_id="target-long-poll-request-2",
            active_claims={first["command_id"]: first["claim_token"]},
        )
        assert second["delivery_kind"] == "command"
        delivered = {
            first["command_id"]: first["claim_token"],
            second["command_id"]: second["claim_token"],
        }
        assert set(delivered) == {"short-probe", "long-ledger-claim"}

        now[0] += 0.25  # only the 0.2 s probe's deadline has passed
        assert not world["mailbox"]._outstanding_claim_deadline_is_live(
            {"short-probe": delivered["short-probe"]}
        )
        assert world["mailbox"]._outstanding_claim_deadline_is_live(
            {"long-ledger-claim": delivered["long-ledger-claim"]}
        )
        started = time.monotonic()
        follow_up = _concurrent_poll(
            world, sequence=3, nonce="target-long-poll-nonce-3",
            request_id="target-long-poll-request-3", active_claims=delivered,
        )
        elapsed = time.monotonic() - started
        assert follow_up["delivery_kind"] == "empty"
        assert elapsed < 1.0, (
            f"a live-deadline claim did not stop the park ({elapsed:.3f}s)"
        )


def test_the_hold_is_bounded_below_the_control_transport_timeout(tmp_path: Path) -> None:
    """The hold's own bound, and the trade-off it is bounded against."""

    from mycelium_a13_macos import managed_control as control

    assert 0 < control._LONG_POLL_STEP_SECONDS < control._LONG_POLL_HOLD_SECONDS
    assert control._LONG_POLL_HOLD_SECONDS <= control._LONG_POLL_MAX_HOLD_SECONDS <= 5.0
    # One pump `step()` blocks for the whole hold, and it must still fit inside
    # the target control client's own 10 s transport timeout with a relay round
    # trip either side of it.
    assert control._LONG_POLL_HOLD_SECONDS + 2.0 <= ManagedControlClient.__init__.__kwdefaults__["timeout"]

    with _world(tmp_path) as world:
        assert world["mailbox"].long_poll_hold == control._LONG_POLL_HOLD_SECONDS
        assert world["mailbox"].long_poll_step == control._LONG_POLL_STEP_SECONDS
        with pytest.raises(
            ManagedControlError, match="managed_control_long_poll_hold_invalid"
        ):
            ManagedControlMailbox(
                tmp_path / "invalid-hold" / "mailbox.sqlite3",
                coordinator=world["authority"].coordinator,
                long_poll_hold=-1.0,
            )
        with pytest.raises(
            ManagedControlError, match="managed_control_long_poll_step_invalid"
        ):
            ManagedControlMailbox(
                tmp_path / "invalid-step" / "mailbox.sqlite3",
                coordinator=world["authority"].coordinator,
                long_poll_step=0.0,
            )
