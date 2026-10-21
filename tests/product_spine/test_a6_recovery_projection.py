# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mycelium_product_spine.app import ProductEvidenceApplication
from mycelium_product_spine.projector import ProductProjector


RAW_RECOVERY = {
    "protocol": "mycelium.full_context_recovery_runtime.v1",
    "binding": {
        "deployment_id": "deployment-a6",
        "deployment_epoch": 8,
        "qualification_digest": "sha256:" + "a" * 64,
        "a4_atomic_commit": "b" * 40,
    },
    "requests": [
        {
            "request_id": "private-request-a6",
            "attempt": 2,
            "path_id": "private-successor-path",
            "path_attempt": 1,
            "phase": "recovered_decode",
            "committed_token_count": 17,
            "recovery_attempt": 1,
            "cleanup_complete": False,
            "reconciled": None,
            "terminal": None,
        }
    ],
    "breaker": {
        "state": "closed",
        "failure_observations_unix_ms": [],
        "open_until_unix_ms": 0,
    },
    "runtime_digest": "sha256:" + "c" * 64,
}


def _project(projector: ProductProjector, recovery=RAW_RECOVERY):
    return projector.project(
        members=[],
        route_status=None,
        qualification=None,
        recovery_status=recovery,
        now_unix_ms=1_500_000,
    )


def test_recovery_is_one_bounded_aggregate_without_private_correlation_ids() -> None:
    snapshot = _project(ProductProjector(pseudonym_salt=b"a" * 32))
    recovery = [entity for entity in snapshot["entities"] if entity["kind"] == "recovery"]

    assert recovery == [{
        "entity_id": "recovery-runtime",
        "kind": "recovery",
        "label": "Full-context replay recovery",
        "source_id": "recovery-source",
        "binding": {
            "deployment_id": "deployment-a6",
            "deployment_epoch": 8,
            "route_id": None,
            "route_generation": None,
            "topology_version": None,
        },
        "freshness": {
            "status": "current",
            "observed_at_unix_ms": 1_500_000,
            "valid_until_unix_ms": 1_500_000,
        },
        "attributes": {
            "request_count": 1,
            "recovering_count": 1,
            "terminal_count": 0,
            "terminal_blocked_count": 0,
            "cleanup_complete_count": 0,
            "reconciled_count": 0,
            "max_recovery_attempt": 1,
            "breaker_state": "closed",
        },
    }]
    # A8 intentionally exposes a reduced endpoint_pseudonym field, not a raw
    # endpoint. Ignore only that allowed key name; still inspect all values.
    encoded = json.dumps(snapshot, sort_keys=True).replace(
        '"endpoint_pseudonym":', '"pseudonym":'
    )
    for forbidden in (
        "private-request-a6", "private-successor-path", "A6-BROWSER-PRIVATE-PROMPT-",
        "A6-BROWSER-PRIVATE-OUTPUT-", "request_id", "path_id",
        "session_id", "node_id", "placement_id", "endpoint", "exception",
        "committed_token_count", "token_ids", "prompt",
    ):
        assert forbidden not in encoded


def test_missing_recovery_source_degrades_only_recovery_scope() -> None:
    snapshot = _project(ProductProjector(pseudonym_salt=b"b" * 32), recovery=None)
    source = next(item for item in snapshot["source_states"] if item["source_id"] == "recovery-source")
    assert source["status"] == "missing"
    assert source["reason_code"] == "recovery_source_missing"
    assert not any(entity["kind"] == "recovery" for entity in snapshot["entities"])


def test_failing_recovery_source_keeps_the_last_aggregate_as_stale() -> None:
    projector = ProductProjector(pseudonym_salt=b"d" * 32)
    current = next(entity for entity in _project(projector)["entities"]
                   if entity["entity_id"] == "recovery-runtime")
    failed = projector.project(
        members=[], route_status=None, qualification=None, recovery_status=None,
        now_unix_ms=1_600_000,
        source_errors={"recovery-source": "recovery_source_failed"},
    )
    source = next(item for item in failed["source_states"]
                  if item["source_id"] == "recovery-source")
    assert source["status"] == "missing"
    assert source["reason_code"] == "recovery_source_failed"
    recovery = [entity for entity in failed["entities"] if entity["kind"] == "recovery"]
    assert recovery == [{**current, "freshness": {
        "status": "stale",
        "observed_at_unix_ms": 1_500_000,
        "valid_until_unix_ms": 1_500_000,
    }}]
    assert failed["publication"]["source_mode"] == "degraded"


def test_failing_recovery_source_without_an_earlier_aggregate_publishes_none() -> None:
    snapshot = ProductProjector(pseudonym_salt=b"e" * 32).project(
        members=[], route_status=None, qualification=None, recovery_status=None,
        now_unix_ms=1_500_000,
        source_errors={"recovery-source": "recovery_source_failed"},
    )
    assert not any(entity["kind"] == "recovery" for entity in snapshot["entities"])


async def _request(app: ProductEvidenceApplication, path: str, cursor: int | None = None):
    sent = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        sent.append(message)
    headers = [] if cursor is None else [(b"last-event-id", str(cursor).encode("ascii"))]
    await app({"type": "http", "method": "GET", "path": path, "headers": headers}, receive, send)
    return sent


def test_recovery_projection_replays_after_restart_and_reconnect(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    def application():
        return ProductEvidenceApplication(
            projector=ProductProjector(pseudonym_salt=b"c" * 32),
            membership_source=lambda: [],
            route_source=lambda: None,
            qualification_source=lambda: None,
            recovery_source=lambda: RAW_RECOVERY,
            clock_unix_ms=lambda: 1_500_000,
            state_root=root,
        )
    first = application()
    first_messages = asyncio.run(_request(first, "/v1/product/snapshot"))
    first_snapshot = json.loads(first_messages[-1]["body"])
    restarted = application()
    replay = asyncio.run(_request(restarted, "/v1/product/events", first_snapshot["publication"]["cursor"] - 1))
    body = b"".join(message.get("body", b"") for message in replay)
    assert b'"kind":"recovery"' in body
    assert b"private-request-a6" not in body
    assert b"private-successor-path" not in body
