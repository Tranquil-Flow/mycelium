from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "contracts" / "compatibility-fixtures"
CANARY = "OBSERVATORY_EVENT_PRIVACY_CANARY_DO_NOT_PUBLISH"
ZERO = "sha256:" + "0" * 64


def fixture_document(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def qualification_bytes(**changes: Any) -> bytes:
    document = fixture_document("route-qualification-v1.json")
    document.update(changes)
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def request_event_bytes(
    request_id: str,
    sequence: int,
    kind: str,
    *,
    token_index: int | None = None,
    text: str | None = None,
    code: str | None = None,
    protocol: str = "mycelium.request_event.v1",
    **extra: Any,
) -> bytes:
    document: dict[str, Any] = {
        "protocol": protocol,
        "request_id": request_id,
        "sequence": sequence,
        "publisher_generation": 1,
        "type": kind,
    }
    if kind == "token":
        document.update(token_index=token_index, text=text)
    elif kind == "failed":
        document["code"] = code
    document.update(extra)
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


@pytest.fixture
def publisher(tmp_path: Path):
    from mycelium_gateway.observatory import CoherentSnapshotPublisher

    return CoherentSnapshotPublisher(
        tmp_path / "observatory-event-state.json",
        replay_capacity=16,
        subscriber_queue_size=2,
    )
