from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mycelium_product_spine import (
    ProductEvidenceApplication,
    ProductEvidenceStateError,
    ProductProjector,
)


ROOT = Path(__file__).resolve().parents[2]


def _sources() -> tuple[list[dict], dict, dict]:
    route = json.loads(
        (ROOT / "contracts/compatibility-fixtures/live-route-status-v1.json").read_text()
    )
    qualification = json.loads(
        (ROOT / "contracts/compatibility-fixtures/route-qualification-v1.json").read_text()
    )
    qualification.update(
        qualification_id="qualification-live",
        route_ready=True,
        issued_at_unix_ms=1_000_000,
        deployment_id=route["deployment_id"],
        deployment_epoch=1,
        topology_version=route["topology_version"],
        model_id=route["model_id"],
        placement_provenance="operator_selected",
        reason_codes=[],
    )
    members = [
        {
            "node_id": node_id,
            "generation": 1,
            "incarnation": "incarnation-1",
            "lease_expires_at": 2_000.0,
            "last_liveness_at": 1_000.0,
            "lifecycle_state": "RUNNING",
            "peer_class": "mac_mlx_iroh",
            "runtime_capability": {
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
            "activation_eligible": True,
        }
        for node_id in ("node-a", "node-b")
    ]
    return members, route, qualification


def _application(state_root: Path | None = None, *, replay_limit: int = 4):
    members, route, qualification = _sources()
    return ProductEvidenceApplication(
        projector=ProductProjector(pseudonym_salt=b"s" * 32),
        membership_source=lambda: members,
        route_source=lambda: route,
        qualification_source=lambda: qualification,
        clock_unix_ms=lambda: 1_500_000,
        replay_limit=replay_limit,
        state_root=None if state_root is None else str(state_root),
    )


async def _request(
    app: ProductEvidenceApplication,
    path: str,
    *,
    last_event_id: int | None = None,
) -> tuple[int, bytes]:
    messages: list[dict] = []
    headers = []
    if last_event_id is not None:
        headers.append((b"last-event-id", str(last_event_id).encode("ascii")))

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": headers,
        },
        receive,
        send,
    )
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    return start["status"], body


def test_unknown_path_does_not_advance_publication_cursor() -> None:
    app = _application()
    status, _ = asyncio.run(_request(app, "/unknown"))
    snapshot_status, body = asyncio.run(_request(app, "/v1/product/snapshot"))

    assert status == 404
    assert snapshot_status == 200
    assert json.loads(body)["publication"]["cursor"] == 1


def test_restart_restores_cursor_and_replay_window(tmp_path: Path) -> None:
    state_root = tmp_path / "private-product-state"
    state_root.mkdir(mode=0o700)
    first = _application(state_root)
    status, body = asyncio.run(_request(first, "/v1/product/snapshot"))
    first_cursor = json.loads(body)["publication"]["cursor"]
    assert status == 200

    restarted = _application(state_root)
    event_status, event_body = asyncio.run(
        _request(restarted, "/v1/product/events", last_event_id=first_cursor)
    )

    assert event_status == 200
    assert f"id: {first_cursor + 1}\n".encode() in event_body
    persisted = json.loads((state_root / "product-evidence-state.v1.json").read_text())
    assert persisted["events"][-1]["cursor"] == first_cursor + 1
    assert (state_root / "product-evidence-state.v1.json").stat().st_mode & 0o777 == 0o600


def test_restart_upgrades_exact_generation_one_device_state(tmp_path: Path) -> None:
    state_root = tmp_path / "private-product-state"
    state_root.mkdir(mode=0o700)
    first = _application(state_root)
    status, _body = asyncio.run(_request(first, "/v1/product/snapshot"))
    assert status == 200
    state_file = state_root / "product-evidence-state.v1.json"
    persisted = json.loads(state_file.read_text())
    for event in persisted["events"]:
        for entity in event["snapshot"]["entities"]:
            if entity["kind"] == "device":
                assert entity["attributes"].pop("authority_generation") == 1
    state_file.write_text(json.dumps(persisted, sort_keys=True, separators=(",", ":")))
    state_file.chmod(0o600)

    restarted = _application(state_root)
    snapshot_status, body = asyncio.run(
        _request(restarted, "/v1/product/snapshot")
    )
    snapshot = json.loads(body)

    assert snapshot_status == 200
    assert {
        entity["attributes"]["authority_generation"]
        for entity in snapshot["entities"]
        if entity["kind"] == "device"
    } == {1}


def test_state_store_rejects_non_private_or_corrupt_state(tmp_path: Path) -> None:
    public_root = tmp_path / "public"
    public_root.mkdir(mode=0o755)
    with pytest.raises(ProductEvidenceStateError, match="product_evidence_state_root_invalid"):
        _application(public_root)

    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    state_file = private_root / "product-evidence-state.v1.json"
    state_file.write_text("not-json")
    state_file.chmod(0o600)
    with pytest.raises(ProductEvidenceStateError, match="product_evidence_state_corrupt"):
        _application(private_root)


def test_failed_authority_is_scoped_degradation_not_whole_endpoint_failure() -> None:
    members, route, qualification = _sources()

    def failed_membership():
        raise RuntimeError("private upstream detail")

    app = ProductEvidenceApplication(
        projector=ProductProjector(pseudonym_salt=b"d" * 32),
        membership_source=failed_membership,
        route_source=lambda: route,
        qualification_source=lambda: qualification,
        clock_unix_ms=lambda: 1_500_000,
    )
    status, body = asyncio.run(_request(app, "/v1/product/snapshot"))
    snapshot = json.loads(body)

    assert status == 200
    assert snapshot["publication"]["source_mode"] == "degraded"
    assert any(entity["kind"] == "route" for entity in snapshot["entities"])
    assert not any(entity["kind"] == "device" for entity in snapshot["entities"])
    assert "private upstream detail" not in body.decode()
    assert any(
        notice["code"] == "membership_source_failed"
        for notice in snapshot["notices"]
    )
