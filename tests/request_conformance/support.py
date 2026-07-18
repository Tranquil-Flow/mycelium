"""Deterministic public-interface fixtures for request conformance tests.

Synthetic RouteQualificationV1 records prove local contract behavior only.  They
do not establish physical qualification or route readiness.
"""

from __future__ import annotations

from dataclasses import fields
import importlib.util
from pathlib import Path
import sys
import threading
from typing import Any, Callable

from mycelium_qualification.evidence import sha256_bytes
from mycelium_qualification.qualifier import qualify_route
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding


ROOT = Path(__file__).resolve().parents[2]


def synthetic_qualification():
    spec = importlib.util.spec_from_file_location(
        "request_conformance_qualification_fixture",
        ROOT / "tests" / "qualification" / "conftest.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    case = module.make_case()
    files, manifest = case.render()

    def verify(statement: bytes, signature: dict[str, Any]) -> bool:
        return (
            signature.get("algorithm") == "ed25519"
            and signature.get("signature")
            == "synthetic-test-signature-never-production"
            and signature.get("signed_statement_digest") == sha256_bytes(statement)
        )

    return qualify_route(
        evidence_files=files,
        evidence_manifest=manifest,
        now_unix_ms=case.now_unix_ms,
        verify_gossip_signature=verify,
        verify_load_proof_signature=verify,
    )


def clone_qualification(qualification, **changes):
    clone = object.__new__(type(qualification))
    for item in fields(qualification):
        object.__setattr__(
            clone,
            item.name,
            changes.get(item.name, getattr(qualification, item.name)),
        )
    return clone


class MutableQualificationSource:
    def __init__(self, value) -> None:
        self.value = value

    def current(self):
        return self.value


class CountingBackend:
    """Public InferenceBackend seam with deterministic resource counters."""

    def __init__(self, script: Callable[..., str]) -> None:
        self._script = script
        self._lock = threading.Lock()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.release = threading.Event()
        self.runtime_starts = 0
        self.backend_cancels = 0
        self.capacity_acquires = 0
        self.capacity_releases = 0
        self.kv_acquires = 0
        self.kv_cleanups = 0
        self.active_capacity = 0
        self.active_kv = 0

    def run(self, request_id, submission, emit_token, is_cancelled):
        with self._lock:
            self.runtime_starts += 1
            self.capacity_acquires += 1
            self.kv_acquires += 1
            self.active_capacity += 1
            self.active_kv += 1
        self.started.set()
        try:
            return self._script(self, request_id, submission, emit_token, is_cancelled)
        finally:
            with self._lock:
                self.active_capacity -= 1
                self.active_kv -= 1
                self.capacity_releases += 1
                self.kv_cleanups += 1
            self.finished.set()

    def cancel(self, request_id: str) -> None:
        del request_id
        with self._lock:
            self.backend_cancels += 1
        self.release.set()

    def counters(self) -> tuple[int, ...]:
        with self._lock:
            return (
                self.runtime_starts,
                self.backend_cancels,
                self.capacity_acquires,
                self.capacity_releases,
                self.kv_acquires,
                self.kv_cleanups,
                self.active_capacity,
                self.active_kv,
            )


def submission(qualification, *, prompt: str = "private fixture prompt", tokens: int = 4):
    return InferenceSubmission(
        prompt=prompt,
        max_new_tokens=tokens,
        qualification=qualification_binding(qualification),
    )


def drain(service, request_id: str):
    subscription = service.subscribe(request_id, last_event_id=None)
    events = []
    try:
        while True:
            event = subscription.next_event(timeout=2)
            if event is None:
                break
            events.append(event)
            subscription.ack(event.sequence)
            if event.kind in {"completed", "cancelled", "failed"}:
                break
    finally:
        subscription.close()
    return tuple(events)
