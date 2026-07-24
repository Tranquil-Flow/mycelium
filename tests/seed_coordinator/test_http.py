from __future__ import annotations

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from pathlib import Path
import json
import threading

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_membership import CAPABILITY_REPORT_PROTOCOL, LEASE_RENEWAL_PROTOCOL
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError, SeedHTTPServer


NOW = 3_000.0


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def test_real_http_join_and_member_message_roundtrip(tmp_path: Path) -> None:
    coordinator = SeedCoordinator(
        swarm_id="swarm-http",
        seed_node_id="seed-node",
        seed_url=None,
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint"),
        invite_registry=SqliteInviteRegistry(tmp_path / "seed" / "invites.sqlite3"),
        incarnation="seed-incarnation",
        clock=lambda: NOW,
        id_source=_ids("seed-message"),
    )
    node = NodeMembershipSession(
        node_id="node-http",
        swarm_id="swarm-http",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(tmp_path / "node" / "identity.key"),
        incarnation="node-incarnation",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=lambda: NOW,
        id_source=_ids("node-message"),
    )

    with SeedHTTPServer(coordinator, host="127.0.0.1", port=0) as server:
        bundle = coordinator.mint_invite(nonce="http-invite", ttl_seconds=120)
        verified = verify_invite_bundle(bundle, now=NOW)
        assert verified["payload"]["seed_url"] == server.base_url
        client = SeedHTTPClient.from_invite_bundle(bundle, now=NOW, timeout=2.0)

        identity = client.identity(now=NOW)
        assert identity["seed_endpoint_id"] == "seed-endpoint"
        request = node.join_request(
            invite_nonce=verified["payload"]["nonce"],
            endpoint_addrs=["https://node-http/control"],
        )
        acceptance = client.join(
            invite_token=bundle["token"],
            join_envelope=request,
        )
        node.accept_join(
            acceptance,
            seed_key_digest=verified["seed_key_digest"],
        )

        capability = node.capability_report(
            platform="macOS-15",
            architecture="arm64",
            memory_bytes=16 * 1024**3,
            available_storage_bytes=80 * 1024**3,
            backends=["mlx"],
            precisions=["float16"],
        )
        receipt = client.send_member_message(capability, now=NOW)
        assert receipt["accepted_message_id"] == capability["message"]["message_id"]
        assert coordinator.member("node-http")["generation"] == 1

        heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
        assert heartbeat is not None
        first_renewal = client.send_member_message(heartbeat, now=NOW)
        retried_renewal = client.send_member_message(heartbeat, now=NOW)
        assert retried_renewal == first_renewal
        assert first_renewal["message"]["protocol"] == LEASE_RENEWAL_PROTOCOL
        assert coordinator.member("node-http")["last_heartbeat_sequence"] == 1

        retry_acceptance = client.join(
            invite_token=bundle["token"],
            join_envelope=request,
        )
        assert retry_acceptance == acceptance


def test_http_server_rejects_noncanonical_or_oversized_json(tmp_path: Path) -> None:
    coordinator = SeedCoordinator(
        swarm_id="swarm-http",
        seed_node_id="seed-node",
        seed_url=None,
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint"),
        invite_registry=SqliteInviteRegistry(tmp_path / "seed" / "invites.sqlite3"),
        incarnation="seed-incarnation",
        clock=lambda: NOW,
    )
    with SeedHTTPServer(coordinator, host="127.0.0.1", port=0) as server:
        host, port_text = server.base_url.removeprefix("http://").split(":")
        connection = HTTPConnection(host, int(port_text), timeout=2)
        noncanonical = b'{ "protocol": "mycelium.seed.member_http.v1" }'
        connection.request(
            "POST",
            "/seed/message",
            body=noncanonical,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(noncanonical)),
            },
        )
        response = connection.getresponse()
        body = response.read()
        assert response.status == 400
        assert json.loads(body)["error"]["code"] == "seed_http_noncanonical"
        connection.close()

        oversized_length = 1024 * 1024 + 2
        connection = HTTPConnection(host, int(port_text), timeout=2)
        connection.putrequest("POST", "/seed/message")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(oversized_length))
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        assert response.status == 413
        assert json.loads(body)["error"]["code"] == "seed_http_frame_too_large"
        connection.close()


def test_http_client_never_redirects_invite_token() -> None:
    leaked_bodies: list[bytes] = []

    class SinkHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            leaked_bodies.append(self.rfile.read(length))
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
    sink_port = sink.server_address[1]

    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):  # noqa: N802
            self.send_response(307)
            self.send_header(
                "Location", f"http://127.0.0.1:{sink_port}/capture"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (sink, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
        client = SeedHTTPClient(
            seed_url=f"http://127.0.0.1:{redirect.server_address[1]}",
            swarm_id="swarm-http",
            seed_key_digest=signer.verification_key_digest,
            seed_key_records=[signer.public_key_record()],
            timeout=2,
        )
        with pytest.raises(SeedHTTPError) as redirected:
            client.join(invite_token="secret-invite-token", join_envelope={})
        assert redirected.value.code == "seed_http_remote_error"
        assert redirected.value.status == 307
        assert leaked_bodies == []
    finally:
        for server in (redirect, sink):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_http_client_rejects_receipt_for_different_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    coordinator = SeedCoordinator(
        swarm_id="swarm-http",
        seed_node_id="seed-node",
        seed_url="http://seed.invalid",
        signer=signer,
        invite_registry=SqliteInviteRegistry(tmp_path / "seed" / "invites.sqlite3"),
        incarnation="seed-incarnation",
        clock=lambda: NOW,
    )
    client = SeedHTTPClient(
        seed_url="http://seed.invalid",
        swarm_id="swarm-http",
        seed_key_digest=signer.verification_key_digest,
        seed_key_records=[signer.public_key_record()],
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: coordinator.receipt_envelope("other-message"),
    )

    with pytest.raises(SeedHTTPError) as mismatch:
        client.send_member_message(
            {"message": {"message_id": "expected-message"}},
            now=NOW,
        )

    assert mismatch.value.code == "seed_http_receipt_mismatch"
