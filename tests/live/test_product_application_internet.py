from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mycelium_internet.contracts import compatibility_fixtures as internet_fixtures
from mycelium_live.supervisor import _product_application


ROOT = Path(__file__).resolve().parents[2]


class _Route:
    def __init__(self, internet_native: dict) -> None:
        self._internet_native = internet_native

    def product_membership_records(self) -> tuple[dict, ...]:
        return ()

    def product_assignment_records(self) -> tuple[dict, ...]:
        return ()

    def product_pseudonym_salt(self) -> bytes:
        return b"p" * 32

    def public_status(self) -> dict:
        return json.loads(
            (ROOT / "contracts/compatibility-fixtures/live-route-status-v1.json").read_text()
        )

    def product_internet_native_snapshot(self) -> dict:
        return self._internet_native


class _Qualification:
    def current(self):
        return None


async def _snapshot(app) -> tuple[int, dict]:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/product/snapshot",
            "query_string": b"",
            "headers": [],
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
    return start["status"], json.loads(body)


def test_supervisor_composes_live_internet_native_source_into_product_snapshot() -> None:
    fixtures = internet_fixtures()
    activation = fixtures["internet-activation-observation-v1.json"]
    expected = {
        "bootstrap_status": fixtures["internet-bootstrap-status-v1.json"],
        "activation_observation": activation,
        "activation_history": [activation],
        "relay_projection": fixtures["relay-projection-v1.json"],
        "qualification": fixtures["internet-native-qualification-v1.json"],
    }

    status, snapshot = asyncio.run(
        _snapshot(_product_application(_Route(expected), _Qualification()))
    )

    assert status == 200
    assert snapshot["internet_native"] == expected
