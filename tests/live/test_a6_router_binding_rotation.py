# SPDX-License-Identifier: AGPL-3.0-only
"""The router's qualification binding follows a cold-restarted route.

``ColdRouteRestartOwner`` replaces a route that lost a peer and publishes the
replacement's fresh qualification as current authority. ``LiveRouterPort``
bound the first admission's digest forever, so after any cold restart every
admission failed with ``qualification_binding_changed`` and the recovery
projection with ``recovery_projection_binding_mismatch``: one source kill
silently ended the serve. The binding may rotate only to the qualification
source's own current record, for the same deployment and graph, and only when
no admitted request would cross the boundary.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import sys

import pytest

from mycelium_qualification.evidence import sha256_bytes
from mycelium_qualification.qualifier import qualify_route

from mycelium_m16_runtime import M16AdmissionError
from mycelium_request_gateway.contracts import (
    InferenceSubmission,
    qualification_binding,
    qualification_digest,
)
from tests.live.test_a6_live_router_recovery import (
    FailingThenRecoveringRoute,
    _graph,
    _runtime,
    _submit_and_drain,
)
from tests.request_gateway.test_core import (
    ROOT,
    MutableQualificationSource,
    _synthetic_qualification,
)


def _replacement(_qualification):
    """The replacement route's record: same deployment/graph, new digest.

    Route-ready records are minted only by the qualifier; re-qualify the same
    synthetic evidence one millisecond later, as a cold restart re-qualifies.
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

    renewed = qualify_route(
        evidence_files=files, evidence_manifest=manifest,
        now_unix_ms=case.now_unix_ms + 1,
        verify_gossip_signature=verify, verify_load_proof_signature=verify,
    )
    assert renewed.route_ready is True
    assert qualification_digest(renewed) != qualification_digest(_qualification)
    return renewed


class _PeekableSource(MutableQualificationSource):
    """An owner-like source: ``peek_current`` is the side-effect-free snapshot.

    ``peeks`` scripts what successive snapshots return, to model a record
    published between two reads.
    """

    def __init__(self, current):
        super().__init__(current)
        self.peeks: list = []

    def peek_current(self):
        return self.peeks.pop(0) if self.peeks else self.value


def _third(first):
    renewed = _replacement(first)
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

    third = qualify_route(
        evidence_files=files, evidence_manifest=manifest,
        now_unix_ms=case.now_unix_ms + 2,
        verify_gossip_signature=verify, verify_load_proof_signature=verify,
    )
    assert len({qualification_digest(x) for x in (first, renewed, third)}) == 3
    return renewed, third


def _setup(tmp_path):
    first = _synthetic_qualification()
    graph = dataclasses.replace(
        _graph(), model_id=first.model_id, resolved_commit=first.resolved_commit)
    source = _PeekableSource(first)
    route = FailingThenRecoveringRoute()
    _coordinator, router, service, _ = _runtime(
        tmp_path, route, base_graph=graph, qualification=first,
        port_qualification_source=source)
    # The gateway admits against the same owned authority the router reads.
    service._gate._source = source
    return first, source, router, service


def test_admission_rotates_to_the_replacement_routes_current_qualification(tmp_path) -> None:
    first, source, router, service = _setup(tmp_path)
    try:
        _request, events = _submit_and_drain(service, first)
        assert [e.kind for e in events if e.kind in {"completed", "failed"}] == ["completed"]
        assert router._recovery_qualification_digest == qualification_digest(first)
        before = router.recovery_public_projection({})
        assert before["binding"]["qualification_digest"] == qualification_digest(first)

        second = _replacement(first)
        source.value = second  # cold restart published the replacement
        # Idle projection follows the current authority instead of failing closed.
        projection = router.recovery_public_projection({})
        assert projection["binding"]["qualification_digest"] == qualification_digest(second)
        assert projection["requests"] == []

        service._request_id_source = lambda: "live-a6-002"
        _submit_and_drain(service, second)
        # Admitted under the rotated binding and dispatched to the route (the
        # simulated route's own source failure afterwards is not under test).
        dispatched = [
            call for call in router._route.calls if call["request_id"] == "live-a6-002"]
        assert dispatched, "admission refused after the route replacement"
        assert router._recovery_qualification_digest == qualification_digest(second)
        assert router._recovery_failures.get("live-a6-002") != "qualification_binding_changed"
    finally:
        service.close()
        router.close()


def test_admission_never_rotates_to_a_digest_that_is_not_current_authority(tmp_path) -> None:
    first, source, router, service = _setup(tmp_path)
    try:
        _submit_and_drain(service, first)
        stranger = _replacement(first)
        # The source still names the first record: a different digest is refused.
        assert router._replacement_binding_digest(qualification_digest(stranger)) is None
        assert router._replacement_binding_digest(qualification_digest(first)) == (
            qualification_digest(first))
        # A current record for another deployment never rotates the binding.
        router._graph = dataclasses.replace(router._graph, deployment_epoch=99)
        source.value = stranger
        assert router._replacement_binding_digest(qualification_digest(stranger)) is None
        with pytest.raises(M16AdmissionError, match="recovery_projection_binding_mismatch"):
            router.recovery_public_projection({})
        assert router._recovery_qualification_digest == qualification_digest(first)
    finally:
        service.close()
        router.close()


def test_projection_does_not_rotate_while_a_request_is_pending(tmp_path) -> None:
    first, source, router, service = _setup(tmp_path)
    try:
        _submit_and_drain(service, first)
        second = _replacement(first)
        source.value = second
        with router._lock:
            router._pending["still-admitted"] = object()
        with pytest.raises(M16AdmissionError, match="recovery_projection_binding_mismatch"):
            router.recovery_public_projection({})
        assert router._recovery_qualification_digest == qualification_digest(first)
        with router._lock:
            router._pending.pop("still-admitted")
        assert router.recovery_public_projection({})["binding"][
            "qualification_digest"] == qualification_digest(second)
    finally:
        service.close()
        router.close()


def test_admission_refuses_a_record_superseded_between_capture_and_rotation(tmp_path) -> None:
    """TOCTOU: the source moves on after the gateway captured the binding."""

    first, source, router, service = _setup(tmp_path)
    try:
        _submit_and_drain(service, first)
        second, third = _third(first)
        source.value = second  # the gateway captures and admits ``second``
        source.peeks = [third]  # ...but the source already publishes ``third``
        service._request_id_source = lambda: "live-a6-002"
        _submit_and_drain(service, second)
        assert not [c for c in router._route.calls if c["request_id"] == "live-a6-002"]
        assert router._recovery_qualification_digest == qualification_digest(first)
    finally:
        service.close()
        router.close()


def test_projection_refuses_a_record_superseded_between_peek_and_rotation(tmp_path) -> None:
    first, source, router, service = _setup(tmp_path)
    try:
        _submit_and_drain(service, first)
        second, third = _third(first)
        source.value = second
        # The projection's first snapshot names ``second``; the locked re-read
        # inside the rotation sees ``third``: nothing is rotated.
        source.peeks = [second, third]
        with pytest.raises(M16AdmissionError, match="recovery_projection_binding_mismatch"):
            router.recovery_public_projection({})
        assert router._recovery_qualification_digest == qualification_digest(first)
    finally:
        service.close()
        router.close()


def test_a_source_without_a_side_effect_free_snapshot_never_rotates(tmp_path) -> None:
    first, source, router, service = _setup(tmp_path)
    try:
        _submit_and_drain(service, first)
        plain = MutableQualificationSource(_replacement(first))
        router._qualification_source = plain
        assert router._replacement_binding_digest(
            qualification_digest(plain.value)) is None
        with router._lock:
            assert router._rotate_binding_locked(
                qualification_digest(plain.value),
                expected_bound=qualification_digest(first)) is False
        assert router._recovery_qualification_digest == qualification_digest(first)
    finally:
        service.close()
        router.close()


def test_rotation_check_and_mutation_are_one_critical_section(tmp_path) -> None:
    """A second rotation cannot interleave between another's check and write."""

    import threading

    first, source, router, service = _setup(tmp_path)
    try:
        _submit_and_drain(service, first)
        second, third = _third(first)
        source.value = second
        in_check = threading.Event()
        release = threading.Event()
        calls = {"n": 0}
        plain_peek = source.peek_current

        def blocking_peek():
            calls["n"] += 1
            if calls["n"] == 2:  # the rotation's own re-read for request A
                in_check.set()
                assert release.wait(5)
            return plain_peek()

        source.peek_current = blocking_peek
        results = {}

        def project(name):
            try:
                results[name] = router.recovery_public_projection({})[
                    "binding"]["qualification_digest"]
            except M16AdmissionError as error:
                results[name] = error.code

        a = threading.Thread(target=project, args=("a",))
        a.start()
        assert in_check.wait(5)
        source.value = third
        b = threading.Thread(target=project, args=("b",))
        b.start()
        b.join(0.3)
        assert b.is_alive(), "a rotation ran while another rotation's check was open"
        release.set()
        a.join(5)
        b.join(5)
        # A validated ``second`` but the source moved on before its locked
        # re-read completed: refused, never bound. B binds the current record.
        assert results["a"] == "recovery_projection_binding_mismatch"
        assert router._recovery_qualification_digest == qualification_digest(third)
        assert results["b"] == qualification_digest(third)
    finally:
        service.close()
        router.close()


def test_projection_stays_bound_while_an_admitted_request_fails_over(tmp_path) -> None:
    # The source kill withdraws the route's qualification while the admitted
    # request is still recovering; its recovery must stay visible.
    first = _synthetic_qualification()
    graph = dataclasses.replace(
        _graph(), model_id=first.model_id, resolved_commit=first.resolved_commit)
    source = _PeekableSource(first)
    route = FailingThenRecoveringRoute(block_successor=True)
    _coordinator, router, service, _ = _runtime(
        tmp_path, route, base_graph=graph, qualification=first,
        port_qualification_source=source)
    service._gate._source = source
    try:
        request_id = service.submit(InferenceSubmission(
            prompt="live recovery prompt", max_new_tokens=4,
            qualification=qualification_binding(first)))
        assert route.successor_started.wait(timeout=2)
        for withdrawn in (None, _replacement(first)):
            source.value = withdrawn
            projection = router.recovery_public_projection({request_id: 2})
            assert projection["binding"]["qualification_digest"] == (
                qualification_digest(first))
            assert [item["phase"] for item in projection["requests"]] == [
                "recovery_prefill"]
        assert router._recovery_qualification_digest == qualification_digest(first)
    finally:
        route.cancelled.set()
        service.close()
        router.close()
