from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading
from typing import Any

import pytest

from mycelium_qualification import QualificationAuthority, QualificationAuthorityError
from mycelium_qualification.qualifier import QualificationError
from mycelium_request_gateway.contracts import qualification_binding
from mycelium_request_gateway.qualification import QualificationGate
from tests.qualification.conftest import make_case, synthetic_signature_verifier


@pytest.fixture
def qualification_case() -> Any:
    return make_case()


@dataclass
class MutableClock:
    value: int | bool

    def __call__(self) -> int | bool:
        return self.value


def _publish(authority: QualificationAuthority, case: Any):
    files, manifest = case.render()
    return authority.qualify_and_publish(
        evidence_files=files,
        evidence_manifest=manifest,
        verify_gossip_signature=synthetic_signature_verifier,
        verify_load_proof_signature=synthetic_signature_verifier,
    )


def test_current_is_absent_before_authority_validates_evidence(qualification_case: Any) -> None:
    clock = MutableClock(qualification_case.now_unix_ms)
    authority = QualificationAuthority(clock_unix_ms=clock)

    assert authority.current() is None


def test_validated_record_is_published_as_exact_immutable_object(qualification_case: Any) -> None:
    clock = MutableClock(qualification_case.now_unix_ms)
    authority = QualificationAuthority(clock_unix_ms=clock)

    record = _publish(authority, qualification_case)

    assert record.route_ready is True
    assert authority.current() is record
    assert not hasattr(authority, "evidence_files")
    with pytest.raises(Exception):
        record.route_ready = False


def test_publication_is_directly_consumable_by_gateway_gate(qualification_case: Any) -> None:
    clock = MutableClock(qualification_case.now_unix_ms)
    authority = QualificationAuthority(clock_unix_ms=clock)
    record = _publish(authority, qualification_case)
    gate = QualificationGate(authority)

    captured = gate.capture(qualification_binding(record))

    assert captured.qualification is record


def test_record_drops_at_exact_evidence_expiry(qualification_case: Any) -> None:
    clock = MutableClock(qualification_case.now_unix_ms)
    authority = QualificationAuthority(clock_unix_ms=clock)
    _publish(authority, qualification_case)

    clock.value = qualification_case.documents["run/route-challenge.json"][
        "valid_until_unix_ms"
    ]

    assert authority.current() is None


def test_failed_requalification_preserves_current_record(qualification_case: Any) -> None:
    clock = MutableClock(qualification_case.now_unix_ms)
    authority = QualificationAuthority(clock_unix_ms=clock)
    record = _publish(authority, qualification_case)
    files, manifest = qualification_case.render()
    files.pop("run/negative-runs.json")

    with pytest.raises(QualificationError):
        authority.qualify_and_publish(
            evidence_files=files,
            evidence_manifest=manifest,
            verify_gossip_signature=synthetic_signature_verifier,
            verify_load_proof_signature=synthetic_signature_verifier,
        )

    assert authority.current() is record


def test_identical_replay_is_idempotent(qualification_case: Any) -> None:
    clock = MutableClock(qualification_case.now_unix_ms)
    authority = QualificationAuthority(clock_unix_ms=clock)
    first = _publish(authority, qualification_case)

    second = _publish(authority, qualification_case)

    assert second is first
    assert authority.current() is first


def test_late_stale_qualification_cannot_replace_newer_record(
    qualification_case: Any,
) -> None:
    clock = MutableClock(qualification_case.now_unix_ms)
    authority = QualificationAuthority(clock_unix_ms=clock)
    files, manifest = qualification_case.render()
    entered = threading.Event()
    release = threading.Event()

    def blocking_verifier(statement: bytes, signature: dict[str, Any]) -> bool:
        entered.set()
        if not release.wait(timeout=5.0):
            raise AssertionError("stale qualification verifier was not released")
        return synthetic_signature_verifier(statement, signature)

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale = executor.submit(
            authority.qualify_and_publish,
            evidence_files=files,
            evidence_manifest=manifest,
            verify_gossip_signature=blocking_verifier,
            verify_load_proof_signature=synthetic_signature_verifier,
        )
        assert entered.wait(timeout=5.0)
        clock.value = qualification_case.now_unix_ms + 1
        newer = _publish(authority, qualification_case)
        release.set()
        with pytest.raises(QualificationAuthorityError) as captured:
            stale.result(timeout=5.0)

    assert captured.value.code == "stale_qualification"
    assert authority.current() is newer


def test_drop_is_compare_and_swap(qualification_case: Any) -> None:
    clock = MutableClock(qualification_case.now_unix_ms)
    authority = QualificationAuthority(clock_unix_ms=clock)
    record = _publish(authority, qualification_case)

    assert authority.drop(expected_qualification_id="wrong") is False
    assert authority.current() is record
    assert authority.drop(expected_qualification_id=record.qualification_id) is True
    assert authority.current() is None
    assert authority.drop(expected_qualification_id=record.qualification_id) is False


def test_clock_rollback_fails_closed_and_drops_current(qualification_case: Any) -> None:
    clock = MutableClock(qualification_case.now_unix_ms)
    authority = QualificationAuthority(clock_unix_ms=clock)
    _publish(authority, qualification_case)
    clock.value = qualification_case.now_unix_ms - 1

    with pytest.raises(QualificationAuthorityError) as captured:
        authority.current()

    assert captured.value.code == "authority_clock_rollback"
    clock.value = qualification_case.now_unix_ms
    assert authority.current() is None


def test_boolean_clock_is_rejected(qualification_case: Any) -> None:
    authority = QualificationAuthority(clock_unix_ms=MutableClock(True))

    with pytest.raises(QualificationAuthorityError) as captured:
        authority.current()

    assert captured.value.code == "invalid_authority_time"
