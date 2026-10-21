# SPDX-License-Identifier: AGPL-3.0-only
"""A10 concurrency probe harness (lane pm-a10): the physical-run consumer.

``scripts/run_a10_concurrency_probe.py`` fires N concurrent requests at a
live origin and reads back whatever ``mycelium.runtime_batch_observation.v1``
evidence exists, evaluating the exact A10 physical-gate predicate against
it. These tests run it against a REAL local HTTP server (a fake origin), not
mocked function objects, so the real networking code path is exercised.

Failure modes this suite catches (one per test, by name):

  test_concurrent_requests_all_reach_the_fake_origin_in_order
      Requests lost, duplicated, or returned out of request-index order.
  test_missing_observation_endpoint_is_reported_honestly_not_as_a_pass
      A 404 from the observation endpoint (today's real deployment state)
      smoothed into an empty-but-successful, gate-satisfied read.
  test_qualifying_observation_satisfies_the_gate
      A real ``backend_invocations == 1`` / ``actual_member_count >= 2``
      observation not being recognised as satisfying the gate.
  test_non_qualifying_observations_never_satisfy_the_gate
      A sequential-dispatch or single-member observation counted as
      satisfying evidence.
  test_malformed_observation_document_is_reported_honestly
      A 200 response whose body is not the documented shape treated as zero
      evidence silently, instead of a distinct, visible failure mode.
  test_artifact_is_valid_json_on_disk_and_matches_the_gate_verdict
      The written JSON artifact drifting from the in-memory verdict, or not
      being valid JSON.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts.run_a10_concurrency_probe import (
    build_artifact,
    evaluate_gate,
    fetch_observation_evidence,
    fire_concurrent_requests,
    run_probe,
    write_artifact,
)


class _FakeOrigin(BaseHTTPRequestHandler):
    """A tiny real HTTP server standing in for the live Mycelium origin."""

    observation_document: dict | bytes | None = None
    observation_status: int = 200
    generate_delay_seconds: float = 0.0

    def log_message(self, *args):  # noqa: D401 - silence test output
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = None
        if self.generate_delay_seconds:
            import time

            time.sleep(self.generate_delay_seconds)
        response = json.dumps({"echo": payload}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):
        if self.observation_document is None:
            self.send_response(404)
            self.end_headers()
            return
        if isinstance(self.observation_document, bytes):
            body = self.observation_document
        else:
            body = json.dumps(self.observation_document).encode("utf-8")
        self.send_response(self.observation_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_origin():
    handler_class = type(f"_FakeOriginHandler{id(object())}", (_FakeOrigin,), {})
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, handler_class
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _url(server) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def test_concurrent_requests_all_reach_the_fake_origin_in_order(fake_origin):
    server, _handler = fake_origin
    outcomes = fire_concurrent_requests(
        _url(server),
        concurrency=8,
        prompt="probe-prompt",
        timeout_seconds=5.0,
    )
    assert len(outcomes) == 8
    for index, outcome in enumerate(outcomes):
        assert outcome["status"] == "ok"
        assert outcome["http_status"] == 200
        assert outcome["body"]["echo"]["request_index"] == index
        assert outcome["body"]["echo"]["prompt"] == "probe-prompt"


def test_missing_observation_endpoint_is_reported_honestly_not_as_a_pass(fake_origin):
    server, handler_class = fake_origin
    handler_class.observation_document = None  # 404, matches today's real deployment
    evidence = fetch_observation_evidence(_url(server) + "/__mycelium/a10/observations", timeout_seconds=5.0)
    assert evidence["source_status"] == "unavailable"
    assert evidence["observations"] == ()
    gate = evaluate_gate(evidence["observations"])
    assert gate["satisfied"] is False
    assert gate["reason"] == "no_observation_claims_a_runtime_batch"


def test_qualifying_observation_satisfies_the_gate(fake_origin):
    server, handler_class = fake_origin
    handler_class.observation_document = {
        "observations": [
            {
                "backend_invocations": 1,
                "proposed_member_count": 2,
                "actual_member_count": 2,
                "membership_authority": "stage_runtime",
            }
        ]
    }
    evidence = fetch_observation_evidence(_url(server) + "/__mycelium/a10/observations", timeout_seconds=5.0)
    assert evidence["source_status"] == "ok"
    gate = evaluate_gate(evidence["observations"])
    assert gate["satisfied"] is True
    assert gate["qualifying_observation_count"] == 1
    assert gate["reason"] is None


def test_non_qualifying_observations_never_satisfy_the_gate(fake_origin):
    server, handler_class = fake_origin
    handler_class.observation_document = {
        "observations": [
            {"backend_invocations": 2, "actual_member_count": 2},  # a split, not a batch
            {"backend_invocations": 1, "actual_member_count": 1},  # below the minimum
            {"backend_invocations": 1, "actual_member_count": True},  # bool is not an int count
        ]
    }
    evidence = fetch_observation_evidence(_url(server) + "/__mycelium/a10/observations", timeout_seconds=5.0)
    gate = evaluate_gate(evidence["observations"])
    assert gate["satisfied"] is False
    assert gate["qualifying_observation_count"] == 0
    assert gate["total_observation_count"] == 3


def test_malformed_observation_document_is_reported_honestly(fake_origin):
    server, handler_class = fake_origin
    handler_class.observation_document = b"not json"
    evidence = fetch_observation_evidence(_url(server) + "/__mycelium/a10/observations", timeout_seconds=5.0)
    assert evidence["source_status"] == "malformed_document"
    assert evidence["observations"] == ()


def test_artifact_is_valid_json_on_disk_and_matches_the_gate_verdict(fake_origin, tmp_path):
    server, handler_class = fake_origin
    handler_class.observation_document = {
        "observations": [
            {"backend_invocations": 1, "actual_member_count": 3},
        ]
    }
    artifact = run_probe(
        origin_url=_url(server),
        observation_url=_url(server) + "/__mycelium/a10/observations",
        concurrency=3,
        prompt="probe-prompt",
        timeout_seconds=5.0,
    )
    assert artifact["gate"]["satisfied"] is True
    assert artifact["request_success_count"] == 3
    out_path = tmp_path / "probe.json"
    write_artifact(out_path, artifact)
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk == artifact
    assert on_disk["gate"]["satisfied"] is True
