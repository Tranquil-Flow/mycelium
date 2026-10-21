# SPDX-License-Identifier: AGPL-3.0-only
"""A finished recovery stays in the public projection across a binding change.

After a source kill the cold-restart owner replaces the route and publishes
the replacement's qualification. The idle projection then rotates the router's
qualification binding (``_rotate_binding_locked``), and an idle renewal binds
a renewed record (``run_qualification_refresh_when_idle``). Both cleared the
request IDs the projection lists, so the request that had just recovered
vanished from ``mycelium.full_context_recovery_runtime.v1``: the A6 browser
matrix, reloading after the fault, never saw its cutover or terminal although
the product had recovered. The A6 spec (section 7) requires refresh and a
clean second session to reconstruct public recovery state, and the browser
gate requires terminal history. Released terminal records are therefore
carried across the binding change, bounded by count and by age; nothing
in flight is carried (a rotation is refused while any request is admitted).
"""
from __future__ import annotations

import importlib.util
import sys

from mycelium_a6_recovery import validate_recovery_runtime
from mycelium_live import router_port
from mycelium_qualification.evidence import sha256_bytes
from mycelium_qualification.qualifier import qualify_route
from mycelium_request_gateway.contracts import qualification_digest
from tests.live.test_a6_live_router_recovery import _submit_and_drain
from tests.live.test_a6_router_binding_rotation import _setup
from tests.request_gateway.test_core import ROOT


def _requalified(offset_ms: int):
    """A route-ready record for the same deployment and graph, minted later.

    Route-ready records are minted only by the qualifier; re-qualifying the
    same synthetic evidence ``offset_ms`` later yields a distinct digest, as a
    cold restart re-qualifies its replacement route.
    """

    spec = importlib.util.spec_from_file_location(
        "request_gateway_qualification_fixture",
        ROOT / "tests" / "qualification" / "conftest.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    case = module.make_case()
    files, manifest = case.render()

    def verify(statement: bytes, signature) -> bool:
        return (
            signature.get("algorithm") == "ed25519"
            and signature.get("signature") == "synthetic-test-signature-never-production"
            and signature.get("signed_statement_digest") == sha256_bytes(statement)
        )

    record = qualify_route(
        evidence_files=files, evidence_manifest=manifest,
        now_unix_ms=case.now_unix_ms + offset_ms,
        verify_gossip_signature=verify, verify_load_proof_signature=verify,
    )
    assert record.route_ready is True
    return record


def _recover(service, router, qualification, request_id: str) -> None:
    """Drive one ordinary request through a source failure to a recovery."""

    router._route.calls.clear()  # the simulated route fails each new source
    service._request_id_source = lambda: request_id
    submitted, events = _submit_and_drain(service, qualification)
    assert submitted == request_id
    assert [e.kind for e in events if e.kind in {"completed", "failed"}] == [
        "completed"], router.recovery_failure_reason(request_id)


def _records(service) -> dict[str, dict]:
    document = validate_recovery_runtime(service.recovery_public_projection())
    return {item["request_id"]: item for item in document["requests"]}


def _binding(service) -> str:
    return service.recovery_public_projection()["binding"]["qualification_digest"]


_RECOVERED = {
    "attempt": 2,
    "phase": "terminal",
    "terminal": "completed",
    "recovery_attempt": 1,
    "cleanup_complete": True,
    "committed_token_count": 4,
}


def _facts(record: dict) -> dict:
    return {key: record[key] for key in _RECOVERED}


def test_a_recovered_request_stays_visible_after_the_route_replacement_rotation(
    tmp_path,
) -> None:
    first, source, router, service = _setup(tmp_path)
    try:
        _recover(service, router, first, "live-a6-001")
        before = _records(service)["live-a6-001"]
        assert _facts(before) == _RECOVERED

        second = _requalified(1)
        source.value = second  # the cold restart published the replacement
        after = _records(service)  # this idle read rotates the binding
        assert router._recovery_qualification_digest == qualification_digest(second)
        assert _binding(service) == qualification_digest(second)
        assert "live-a6-001" in after, "the recovered request vanished at rotation"
        assert after["live-a6-001"] == before  # cutover and attempt facts kept
        assert _records(service)["live-a6-001"] == before  # repeated reads too

        # A request admitted under the replacement binding is listed beside it.
        _recover(service, router, second, "live-a6-002")
        both = _records(service)
        assert set(both) == {"live-a6-001", "live-a6-002"}
        assert both["live-a6-001"] == before
        assert _facts(both["live-a6-002"]) == _RECOVERED
    finally:
        service.close()
        router.close()


def test_a_recovered_request_stays_visible_after_an_idle_renewal(tmp_path) -> None:
    first, source, router, service = _setup(tmp_path)
    try:
        _recover(service, router, first, "live-a6-001")
        before = _records(service)["live-a6-001"]
        renewed = _requalified(1)
        source.value = renewed
        router.run_qualification_refresh_when_idle(lambda: renewed)
        assert router._recovery_qualification_digest == qualification_digest(renewed)
        after = _records(service)
        assert _binding(service) == qualification_digest(renewed)
        assert after.get("live-a6-001") == before
    finally:
        service.close()
        router.close()


def test_carried_terminal_records_are_bounded_by_count_oldest_first(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        router_port, "_ROTATED_TERMINAL_RECORDS_MAX", 2, raising=False)
    first, source, router, service = _setup(tmp_path)
    try:
        for request_id in ("live-a6-001", "live-a6-002", "live-a6-003"):
            _recover(service, router, first, request_id)
        assert set(_records(service)) == {
            "live-a6-001", "live-a6-002", "live-a6-003"}

        source.value = _requalified(1)
        # Only the two most recently finished records cross the rotation.
        assert set(_records(service)) == {"live-a6-002", "live-a6-003"}
        assert len(router._recovery_rotated_terminal_ids) == 2

        # A second rotation re-carries the carried set and stays bounded.
        _recover(service, router, source.value, "live-a6-004")
        source.value = _requalified(2)
        assert set(_records(service)) == {"live-a6-003", "live-a6-004"}
        assert len(router._recovery_rotated_terminal_ids) == 2
    finally:
        service.close()
        router.close()


def test_carried_terminal_records_expire_by_age(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        router_port, "_ROTATED_TERMINAL_RECORD_MAX_AGE_MS", 5_000, raising=False)
    first, source, router, service = _setup(tmp_path)
    try:
        _recover(service, router, first, "live-a6-001")
        now = {"ms": 10_000}
        router._recovery_clock_ms = lambda: now["ms"]
        source.value = _requalified(1)
        assert "live-a6-001" in _records(service)  # carried at 10_000
        now["ms"] = 15_000  # exactly the bound: still visible
        assert "live-a6-001" in _records(service)
        now["ms"] = 15_001
        assert "live-a6-001" not in _records(service)
        assert len(router._recovery_rotated_terminal_ids) == 0  # pruned, not hidden
        now["ms"] = 10_000  # a clock step back never resurrects it
        assert "live-a6-001" not in _records(service)
    finally:
        service.close()
        router.close()


def test_the_production_bounds_are_finite_and_tight() -> None:
    assert 0 < router_port._ROTATED_TERMINAL_RECORDS_MAX <= 256
    assert 0 < router_port._ROTATED_TERMINAL_RECORD_MAX_AGE_MS <= (
        router_port._MAX_RECOVERY_DEADLINE_MS)
