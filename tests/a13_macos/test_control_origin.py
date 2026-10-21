# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import socket
import time
from typing import Any, Iterator, Mapping

import pytest

from mycelium_a13_macos.authority import DemoAuthority
from mycelium_a13_macos.control_origin import ControlOriginServer
from mycelium_a13_macos.managed_control import (
    CONTROL_COMMAND_PROTOCOL,
    ManagedControlClient,
    ManagedControlMailbox,
)
from mycelium_a13_macos.peer import RemoteMacMember
from mycelium_a13_macos.tls import pinned_seed_client, pinned_seed_client_from_invite
from mycelium_node.identity import load_node_signer
from mycelium_qualification.evidence import canonical_json_bytes


PUBLIC_HOST = "control.mycelium.now"
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
RUN_ID = "a13-control-origin-run-1"
DEPLOYMENT_ID = "a13-control-origin-deployment-1"


@contextmanager
def _world(tmp_path: Path, *, control_clock=time.time) -> Iterator[dict[str, Any]]:
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
        assert membership is not None
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
    mailbox = ManagedControlMailbox(
        tmp_path / "control" / "mailbox.sqlite3",
        coordinator=authority.coordinator,
        clock=control_clock,
    )
    owner_builder = ManagedControlClient(
        base_url=f"https://{PUBLIC_HOST}",
        signer=owner_signer,
        trusted_owner_key_record=owner_signer.public_key_record(),
        swarm_id=authority.coordinator.swarm_id,
        owner_node_id=authority.coordinator.seed_node_id,
        clock=control_clock,
    )
    target_builder = ManagedControlClient(
        base_url=f"https://{PUBLIC_HOST}",
        signer=target_signer,
        trusted_owner_key_record=owner_signer.public_key_record(),
        swarm_id=authority.coordinator.swarm_id,
        owner_node_id=authority.coordinator.seed_node_id,
        clock=control_clock,
    )
    try:
        yield {
            "authority": authority,
            "target": target,
            "member": member,
            "mailbox": mailbox,
            "owner_builder": owner_builder,
            "target_builder": target_builder,
        }
    finally:
        mailbox.close()
        target.stop()
        authority.stop()


@contextmanager
def _origin(
    mailbox: ManagedControlMailbox,
    *,
    allow_loopback_hosts: bool = False,
    max_concurrent: int = 16,
) -> Iterator[ControlOriginServer]:
    origin = ControlOriginServer(
        mailbox,
        public_host=PUBLIC_HOST,
        allow_loopback_hosts=allow_loopback_hosts,
        max_concurrent=max_concurrent,
    ).start()
    try:
        yield origin
    finally:
        origin.close()


def _command(command_id: str, operation: str = "hello", payload=None) -> dict[str, Any]:
    return {
        "protocol": CONTROL_COMMAND_PROTOCOL,
        "command_id": command_id,
        "run_id": RUN_ID,
        "deployment_id": DEPLOYMENT_ID,
        "command": operation,
        "payload": {} if payload is None else payload,
    }


def _request(
    origin: ControlOriginServer,
    method: str,
    path: str,
    document: Mapping[str, Any] | None = None,
    *,
    host: str = PUBLIC_HOST,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any], bytes]:
    body = None if document is None else canonical_json_bytes(dict(document))
    request_headers = {"Host": host}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers is not None:
        request_headers.update(dict(headers))
    connection = http.client.HTTPConnection("127.0.0.1", origin.port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, response_headers, json.loads(raw), raw
    finally:
        connection.close()


def _raw_request(origin: ControlOriginServer, request: bytes) -> tuple[int, dict[str, Any]]:
    with socket.create_connection(("127.0.0.1", origin.port), timeout=5) as client:
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    head, body = raw.split(b"\r\n\r\n", 1)
    status = int(head.split(b" ", 2)[1])
    return status, json.loads(body)


def _owner_envelope(world: dict[str, Any], command_id: str, *, sequence: int = 1) -> dict[str, Any]:
    statement = world["owner_builder"].owner_command_statement(
        target=world["member"],
        native_command=_command(command_id),
        sequence=sequence,
        nonce=f"owner-nonce-{sequence}",
        request_id=f"owner-request-{sequence}",
    )
    return world["owner_builder"]._signed_outbound(statement)


def _target_poll_envelope(world: dict[str, Any], *, sequence: int = 1) -> dict[str, Any]:
    statement = world["target_builder"]._target_statement(
        protocol="mycelium.managed_control.target_poll.v1",
        target=world["member"],
        run_id=RUN_ID,
        deployment_id=DEPLOYMENT_ID,
        sequence=sequence,
        nonce=f"target-nonce-{sequence}",
        request_id=f"target-request-{sequence}",
        ttl_seconds=60.0,
    )
    return world["target_builder"]._signed_outbound(statement)


def test_healthz_is_host_gated_canonical_no_store_and_privacy_clean(tmp_path: Path) -> None:
    with _world(tmp_path) as world, _origin(
        world["mailbox"], allow_loopback_hosts=True
    ) as origin:
        assert origin.server_address[0] == "127.0.0.1"
        status, headers, document, raw = _request(origin, "GET", "/healthz")
        assert status == 200
        assert headers["cache-control"] == "no-store"
        assert headers["connection"] == "close"
        assert canonical_json_bytes(document) == raw
        assert document == {
            "ok": True,
            "protocol": "mycelium.managed_control.origin_health.v1",
            "service": "managed-control-origin",
        }
        forbidden_fragments = (
            "key",
            "token",
            "capability",
            "command",
            "claim",
            "database",
            "path",
            "member",
        )
        assert not any(fragment in raw.decode("utf-8") for fragment in forbidden_fragments)

        loopback_status, _, loopback_document, _ = _request(
            origin,
            "GET",
            "/healthz",
            host=f"127.0.0.1:{origin.port}",
        )
        assert loopback_status == 200
        assert loopback_document["ok"] is True


def test_signed_enqueue_and_target_poll_flow_through_control_origin(tmp_path: Path) -> None:
    with _world(tmp_path) as world, _origin(world["mailbox"]) as origin:
        enqueue_status, _, enqueue_doc, _ = _request(
            origin,
            "POST",
            "/managed-control/enqueue",
            _owner_envelope(world, "origin-hello-1"),
        )
        assert enqueue_status == 200
        enqueue_statement = enqueue_doc["statement"]
        assert enqueue_statement["protocol"] == "mycelium.managed_control.enqueue_receipt.v1"
        assert enqueue_statement["command_id"] == "origin-hello-1"
        assert enqueue_statement["state"] == "pending"

        poll_status, _, poll_doc, _ = _request(
            origin,
            "POST",
            "/managed-control/poll",
            _target_poll_envelope(world),
        )
        assert poll_status == 200
        poll_statement = poll_doc["statement"]
        assert poll_statement["protocol"] == "mycelium.managed_control.poll_receipt.v1"
        assert poll_statement["delivery_kind"] == "command"
        assert poll_statement["command_id"] == "origin-hello-1"
        assert poll_statement["command_envelope"]["statement"]["native_command"] == _command(
            "origin-hello-1"
        )


def test_host_mismatch_is_rejected_before_mailbox_dispatch(tmp_path: Path) -> None:
    with _world(tmp_path) as world, _origin(world["mailbox"]) as origin:
        def forbidden_dispatch(_method: str, _path: str, _body: Mapping[str, Any]):
            raise AssertionError("mailbox dispatch should not run for a bad Host header")

        world["mailbox"].handle_http = forbidden_dispatch  # type: ignore[method-assign]
        status, _, document, _ = _request(
            origin,
            "POST",
            "/managed-control/enqueue",
            {},
            host="evil.invalid",
        )
        assert status == 403
        assert document == {"error": {"code": "managed_control_origin_host_invalid"}}


@pytest.mark.parametrize(
    "headers,expected_code",
    [
        ({"Cookie": "session=leak"}, "managed_control_headers_invalid"),
        ({"Authorization": "Bearer nope"}, "managed_control_headers_invalid"),
        ({"Transfer-Encoding": "chunked"}, "managed_control_headers_invalid"),
        ({"Content-Type": "text/plain"}, "managed_control_content_type_invalid"),
    ],
)
def test_forbidden_headers_and_content_type_are_rejected(
    tmp_path: Path,
    headers: Mapping[str, str],
    expected_code: str,
) -> None:
    with _world(tmp_path) as world, _origin(world["mailbox"]) as origin:
        status, _, document, _ = _request(
            origin,
            "POST",
            "/managed-control/enqueue",
            {},
            headers=headers,
        )
        assert status == 400
        assert document == {"error": {"code": expected_code}}


def test_content_length_and_route_method_refusals_are_closed_and_named(tmp_path: Path) -> None:
    with _world(tmp_path) as world, _origin(world["mailbox"]) as origin:
        duplicate_length = (
            f"POST /managed-control/enqueue HTTP/1.1\r\n"
            f"Host: {PUBLIC_HOST}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 2\r\n"
            "Content-Length: 2\r\n"
            "Connection: close\r\n"
            "\r\n{}"
        ).encode("ascii")
        status, document = _raw_request(origin, duplicate_length)
        assert status == 400
        assert document == {"error": {"code": "managed_control_frame_invalid"}}

        missing_length = (
            f"POST /managed-control/enqueue HTTP/1.1\r\n"
            f"Host: {PUBLIC_HOST}\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        status, document = _raw_request(origin, missing_length)
        assert status == 400
        assert document == {"error": {"code": "managed_control_frame_invalid"}}

        unknown_status, _, unknown_document, _ = _request(
            origin,
            "POST",
            "/managed-control/unknown",
            {},
        )
        assert unknown_status == 404
        assert unknown_document == {"error": {"code": "managed_control_route_unknown"}}

        method_status, _, method_document, _ = _request(
            origin,
            "GET",
            "/managed-control/enqueue",
        )
        assert method_status == 405
        assert method_document == {"error": {"code": "managed_control_method_invalid"}}


def test_loopback_origin_does_not_expose_seed_invite_or_browser_routes(tmp_path: Path) -> None:
    with _world(tmp_path) as world, _origin(world["mailbox"]) as origin:
        for method, path in (
            ("GET", "/"),
            ("GET", "/invite"),
            ("POST", "/seed/join"),
            ("GET", "/operator"),
        ):
            status, _, document, _ = _request(origin, method, path, {})
            assert status == 404
            assert document == {"error": {"code": "managed_control_route_unknown"}}


def test_capacity_exhaustion_returns_named_503(tmp_path: Path) -> None:
    with _world(tmp_path) as world, _origin(
        world["mailbox"], max_concurrent=1
    ) as origin:
        assert origin._server.slots.acquire(blocking=False)  # direct capacity seam
        try:
            status, _, document, _ = _request(origin, "GET", "/healthz")
        finally:
            origin._server.slots.release()
        assert status == 503
        assert document == {"error": {"code": "managed_control_origin_capacity_exhausted"}}
