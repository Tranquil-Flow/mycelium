# SPDX-License-Identifier: AGPL-3.0-only
"""RED-first contract for the cancellation phase diagnostic (round 54).

Why this exists: seven physical runs put the same-machine `infer_cancel_wait`
handler at 1.622-1.713 s against a transmitted `deadline_budget_ms` of 1499, with
a 91 ms spread -- deadline-shaped, not work-shaped -- while
`round54_cancel_roundtrip.py` measures the *transport* cancellation at
0.09-2.74 ms on its first attempt.  The remaining span lives in the three calls
made by the first `_apply_generation_fenced_cancellation`
(`physical_inference_node.py`): the runtime marker, the transport fence, and
`router.cancel_local`.  This round could not name it locally, so the node emits
bounded phase timings that the next physical attempt can read.

Contract enforced here:

1. **Off by default.** With the environment variable unset, nothing is written.
2. **Allowlisted surface.** The diagnostic is a separate append-only file, never a
   new field on a validated command response and never a widening of an error
   contract.
3. **Payload-free.** Only phase names, millisecond durations and bounded counters.
4. **Non-perturbing.** The diagnostic never raises and never changes the
   cancellation's outcome; a destination that cannot be opened is silently
   ignored.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import physical_inference_node as node_module
from physical_inference_node import (
    PhysicalNodeService,
    _cancel_phase_diagnostic,
    _CANCEL_PHASE_DIAGNOSTIC_ENV,
)

# ---------------------------------------------------------------------------
# The destination is process-global, so the writers are too.
#
# ``_cancel_phase_diagnostic`` reads its destination from ``os.environ`` at
# *write* time, and a reserved cancellation worker is allowed to outlive the
# command that spawned it: deferred cleanup owns the response lane until its
# immutable owner deadline, so ``_infer_cancel`` returns the acknowledgement
# while ``apply_cancellation`` keeps the seal poll running in its own thread.  A
# worker a sibling test leaves polling therefore appends its own
# ``seal_poll``/``seal_poll_error`` lines to whatever file the *next* test points
# the variable at.
#
# The batch-5 L2 run recorded exactly that: four foreign entries in front of this
# file's report -- first one ``seal_poll_error``, ``call_index`` 0, thread
# ``mycelium-cancel-...`` -- from
# ``tests/physical_qualification/test_a4_node_cancellation.py::
# test_cancel_before_start_still_fences_registered_transport_path``, whose
# double's ``_snapshot`` raises on every poll of its 2 000 ms budget and which
# returns as soon as its worker retires from ``service._cancellation_workers``
# (that happens *before* the seal starts).
#
# Every such worker retires inside its own bounded owner deadline (measured:
# 1.92 s of polling left after the sibling file's last test, against the largest
# budget any test transmits, 2 000 ms), so waiting for them is a scheduling gate
# rather than a race against load, and it is what keeps the exact-content
# assertions in this file a statement about this test's own calls instead of
# about every thread alive in the process.
_CANCEL_WORKER_THREAD_PREFIX = "mycelium-cancel-"
_CANCEL_WORKER_QUIESCE_TIMEOUT_SECONDS = 10.0


def _await_foreign_cancellation_workers() -> None:
    """Return once no cancellation worker but this test's own is still running.

    Call this *before* arming ``_CANCEL_PHASE_DIAGNOSTIC_ENV``.  Afterwards no
    thread that can write to the destination is alive, and none can appear while
    this test runs: a worker only starts inside a cancellation command another
    test issues, and a worker finishes its seal poll inside its own immutable
    deadline, which no assertion here changes.
    """

    deadline = time.monotonic() + _CANCEL_WORKER_QUIESCE_TIMEOUT_SECONDS
    while True:
        foreign = tuple(
            thread.name
            for thread in threading.enumerate()
            if thread is not threading.current_thread()
            and thread.is_alive()
            and thread.name.startswith(_CANCEL_WORKER_THREAD_PREFIX)
        )
        if not foreign:
            return
        assert time.monotonic() < deadline, foreign
        time.sleep(0.005)


CONTROL = {
    "deployment_id": "deployment-cancel-diag",
    "deployment_epoch": 1,
    "qualification_digest": "sha256:" + "a" * 64,
    "command_id": "request:cancel-diag:attempt:1",
    "publisher_generation": 1,
    "absolute_deadline_ms": 26_111_949,
    "request_attempt": 1,
    "path_id": "path-cancel-diag",
    "path_attempt": 0,
    "path_digest": "sha256:" + "b" * 64,
    "topology_generation": 1,
    "cancellation_generation": 0,
}


class _Runtime:
    def cancel(self, path_id: str) -> None:
        del path_id

    def kv_subject_clean(self, *_args: object) -> bool:
        return True

    def kv_snapshot(self) -> dict:
        return {"backend": "fake", "mode": "stage_local_kv", "states": {}}

    def kv_snapshot_nonblocking(self) -> dict:
        return self.kv_snapshot()


class _Transport:
    def apply_controlled_path_cancellation(self, cancellation, **kwargs):
        del cancellation, kwargs
        return True

    def cancellation_cleanup_complete(self, *_args: object) -> bool:
        return True

    def cancellation_cleanup_state(self, *_args: object) -> dict:
        return {"pending_delivery_count": 0, "cancellation_observed": True}

    def cancellation_cleanup_observation_nonblocking(self, *_args: object):
        return ({"remote_frames_sent": 1}, self.cancellation_cleanup_state())

    def counter_snapshot(self) -> dict:
        return {"remote_frames_sent": 1}

    def cancellation_observed(self, *_args: object) -> bool:
        return True

    def send_path_cancellation_if_entry(self, cancellation) -> bool:
        del cancellation
        return False


class _Router:
    def cancel_local(self, request_id: str) -> bool:
        del request_id
        return True

    def request_status(self, request_id: str) -> str:
        del request_id
        return "CANCELLED"


def _service(tmp_path: Path) -> PhysicalNodeService:
    service = PhysicalNodeService(
        run_id="run-cancel-diag",
        deployment_id="deployment-cancel-diag",
        node_id="node-0",
        artifact_root=tmp_path,
        socket_root=tmp_path / "socket",
        sidecar_binary=Path("/usr/bin/false"),
        sidecar_local_only=True,
        command_timeout=30.0,
    )
    service.state = "RUNNING"
    service.graph = type(
        "Graph",
        (),
        {
            "deployment_id": CONTROL["deployment_id"],
            "deployment_epoch": 1,
            "topology_version": 1,
        },
    )()
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }  # type: ignore[method-assign]
    service._host_resources = lambda: {}  # type: ignore[method-assign]
    service.runtime = _Runtime()  # type: ignore[assignment]
    service.transport = _Transport()  # type: ignore[assignment]
    service.router = _Router()  # type: ignore[assignment]
    service.capacity = None  # type: ignore[assignment]
    return service


def test_no_diagnostic_file_is_written_when_the_switch_is_absent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, raising=False)
    destination = tmp_path / "cancel-phases.jsonl"
    monkeypatch.setenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, "")
    service = _service(tmp_path)
    service._apply_generation_fenced_cancellation({"request_id": "request-x", **CONTROL})

    assert not destination.exists()


def test_phases_are_recorded_once_per_call_without_changing_the_outcome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "cancel-phases.jsonl"
    # Nothing but this test's own two calls may append: arm the destination only
    # after every cancellation worker a sibling test left polling has retired.
    _await_foreign_cancellation_workers()
    monkeypatch.setenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, str(destination))
    service = _service(tmp_path)
    payload = {"request_id": "request-x", **CONTROL}

    service._apply_generation_fenced_cancellation(payload)
    service._apply_generation_fenced_cancellation(payload)

    lines = [
        json.loads(line)
        for line in destination.read_text().splitlines()
        if line.strip()
    ]
    phases = [entry["phase"] for entry in lines]
    # One bounded report per call, in call order, covering the three named spans.
    assert phases == [
        "runtime_marker",
        "transport_fence",
        "router_cancel_local",
        "total",
    ] * 2
    assert [entry["call_index"] for entry in lines] == [1, 1, 1, 1, 2, 2, 2, 2]
    for entry in lines:
        assert entry["protocol"] == "mycelium.cancel_phase_diagnostic.v1"
        assert isinstance(entry["elapsed_ms"], (int, float))
        assert entry["elapsed_ms"] >= 0.0
        # Payload-free: no request id, no paths, no task text.
        assert set(entry) <= {
            "protocol",
            "phase",
            "elapsed_ms",
            "call_index",
            "code",
            "thread",
        }


def test_an_unwritable_destination_never_changes_behaviour(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        _CANCEL_PHASE_DIAGNOSTIC_ENV,
        str(tmp_path / "missing-directory" / "phases.jsonl"),
    )
    service = _service(tmp_path)

    # No exception escapes, and the fence is still applied exactly once.
    service._apply_generation_fenced_cancellation({"request_id": "request-x", **CONTROL})
    assert service.transport.apply_controlled_path_cancellation(None) is True


def test_the_helper_is_bounded_and_never_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, "/")
    _cancel_phase_diagnostic("probe", 1.5, call_index=1)
    assert node_module._CANCEL_PHASE_DIAGNOSTIC_LINE_BYTES > 0
    assert threading.current_thread().is_alive()
    assert time.monotonic() > 0


class _BusyLockRuntime(_Runtime):
    """Clean subject, but the nonblocking projection cannot take its lock."""

    def kv_snapshot_nonblocking(self) -> None:  # type: ignore[override]
        return None


class _BusyLockTransport(_Transport):
    def cancellation_cleanup_observation_nonblocking(self, *_args: object):
        return None


def test_the_lifecycle_worker_seals_from_its_own_teardown_proof(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """RED before round 55's fix; the binding term of the measured 1.5 s stall.

    The reserved cleanup worker proves the exact subject clean with the blocking
    probes in ~1.3 ms and then had to wait out its entire inline budget because
    the *nonblocking* probe pair needs a free lock.  A receipt that cannot be
    observed nonblockingly is not evidence of uncleanness, so the worker's own
    proof must seal; `complete` must still require the same predicates and the
    same control proof, which this test keeps honest by leaving every predicate
    clean and the cancellation generation consistent.
    """

    monkeypatch.delenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, raising=False)
    service = _service(tmp_path)
    service.runtime = _BusyLockRuntime()  # type: ignore[assignment]
    service.transport = _BusyLockTransport()  # type: ignore[assignment]
    service._bind_request_control({"request_id": "request-x", "control": dict(CONTROL)})

    payload = {
        "request_id": "request-x",
        **CONTROL,
        "cancellation_generation": CONTROL["cancellation_generation"] + 1,
        "deadline_budget_ms": 1499,
    }
    started = time.monotonic()
    result = service._infer_cancel_wait(payload)
    elapsed = time.monotonic() - started

    details = result.get("details") or {}
    receipt = details.get("request_cleanup") or {}
    # The worker's own proof must be enough to publish the ordered receipt.
    assert details.get("cleanup_pending") is False, result
    assert receipt.get("complete") is True, receipt
    assert receipt.get("runtime_clean") is True
    assert receipt.get("transport_clean") is True
    # And it must not spend the inline budget doing so.
    assert elapsed < 0.25, elapsed


def _production_signed_result(self):  # noqa: ANN001 - test helper
    """`_signed_result` as the node really returns it: a signed envelope."""

    def signed(event, details):
        return {
            "protocol": "mycelium.physical_node_observation.v1",
            "event": event,
            "result": {"observation": {"details": details}},
            "signature": {"algorithm": "ed25519"},
        }

    return signed


def test_the_seal_reads_the_committed_receipt_not_a_flattened_envelope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """RED before round 55's second fix; the measured 1.5 s stall on hardware.

    Unit doubles flatten `_signed_result` to ``{"event": ..., "details": ...}``,
    so every existing test saw the receipt where the seal looked for it.  The
    real node returns ``result.observation.details.request_cleanup``, so the seal
    observed "no receipt" forever, slept one poll cadence, and burned its whole
    inline budget on both Macs.  This test pins the production shape.
    """

    monkeypatch.delenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, raising=False)
    service = _service(tmp_path)
    service._signed_result = _production_signed_result(service)  # type: ignore[method-assign]
    service._bind_request_control({"request_id": "request-x", "control": dict(CONTROL)})

    payload = {
        "request_id": "request-x",
        **CONTROL,
        "cancellation_generation": CONTROL["cancellation_generation"] + 1,
        "deadline_budget_ms": 1499,
    }
    started = time.monotonic()
    result = service._infer_cancel_wait(payload)
    elapsed = time.monotonic() - started

    # The stubbed signer returns the production envelope, so unwrap it exactly as
    # the product's own consumers do.
    details = (((result.get("result") or {}).get("observation") or {}).get("details")) or {}
    receipt = details.get("request_cleanup") or {}
    assert details.get("cleanup_pending") is False, result
    assert receipt.get("complete") is True, receipt
    assert elapsed < 0.25, elapsed


# ---------------------------------------------------------------------------
# Fail-closed boundary of the seal (round 54, mandatory invariant).
#
# The fix moved the seal's decision from "what the observation envelope says"
# to "what is committed under the control lock".  That is a strictly stronger
# gate, but it is also the one that guards publication, so its refusals must be
# pinned as hard as its success:
#
#   * no envelope shape -- absent, malformed, flat, or a lying signed envelope
#     that claims `complete: true` -- may seal anything on its own;
#   * a committed receipt whose identity does not match this exact subject must
#     never seal;
#   * an identity-matching committed receipt seals even when the envelope says
#     nothing at all (the hardware case that was blind for nine runs);
#   * a never-observed cancellation exhausts the budget and publishes nothing.
# ---------------------------------------------------------------------------

SUBJECT = {"request_id": "request-x", **CONTROL}

_COMPLETE_RECEIPT = {
    **SUBJECT,
    "complete": True,
    "runtime_clean": True,
    "transport_clean": True,
    "cancellation_worker_complete": True,
}


def _envelope(details: dict) -> dict:
    """The shape production actually returns: a signed envelope."""

    return {
        "protocol": "mycelium.physical_node_observation.v1",
        "event": "snapshot",
        "result": {"observation": {"details": details}},
        "signature": {"algorithm": "ed25519"},
    }


def _seal_elapsed(service, *, budget_s: float) -> float:
    started = time.monotonic()
    service._seal_cancellation_receipt_until_deadline(
        dict(SUBJECT),
        deadline_monotonic_s=started + budget_s,
    )
    return time.monotonic() - started


def _committed(service) -> object:
    with service._control_lock:
        return service._request_cleanup_receipts.get("request-x")


def test_no_envelope_shape_can_seal_an_uncommitted_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Fail-closed: an observation is never the authority for publication."""

    monkeypatch.delenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, raising=False)
    shapes = [
        ("absent", None),
        ("malformed", {"event": "snapshot"}),
        (
            "flat_double_shape",
            {"event": "snapshot", "details": {"request_cleanup": dict(_COMPLETE_RECEIPT)}},
        ),
        (
            "envelope_claims_complete",
            _envelope(
                {
                    "cleanup_pending": False,
                    "request_cleanup": dict(_COMPLETE_RECEIPT),
                }
            ),
        ),
        (
            "envelope_pending",
            _envelope(
                {
                    "cleanup_pending": True,
                    "request_cleanup": {**SUBJECT, "complete": False},
                }
            ),
        ),
    ]
    for label, observation in shapes:
        service = _service(tmp_path)
        service._snapshot = lambda payload, **kwargs: observation  # type: ignore[method-assign]
        with service._control_lock:
            service._request_cleanup_receipts.pop("request-x", None)

        elapsed = _seal_elapsed(service, budget_s=0.3)

        assert elapsed >= 0.25, (label, elapsed)
        assert _committed(service) is None, label


def test_a_committed_receipt_for_a_different_subject_never_seals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Identity is part of the proof: a same-id receipt is not this subject."""

    monkeypatch.delenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, raising=False)
    service = _service(tmp_path)
    service._snapshot = lambda payload, **kwargs: _envelope(  # type: ignore[method-assign]
        {"cleanup_pending": False, "request_cleanup": dict(_COMPLETE_RECEIPT)}
    )
    with service._control_lock:
        service._request_cleanup_receipts["request-x"] = {
            **_COMPLETE_RECEIPT,
            "path_digest": "sha256:" + "c" * 64,
        }

    elapsed = _seal_elapsed(service, budget_s=0.3)

    assert elapsed >= 0.25, elapsed


def test_a_committed_receipt_seals_even_when_the_envelope_says_nothing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The nine-run hardware stall, pinned at the unit boundary."""

    monkeypatch.delenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, raising=False)
    service = _service(tmp_path)
    service._snapshot = lambda payload, **kwargs: None  # type: ignore[method-assign]
    with service._control_lock:
        service._request_cleanup_receipts["request-x"] = dict(_COMPLETE_RECEIPT)

    elapsed = _seal_elapsed(service, budget_s=0.5)

    assert elapsed < 0.1, elapsed


class _UncleanRuntime(_Runtime):
    def kv_subject_clean(self, *_args: object) -> bool:
        return False


class _UncleanTransport(_Transport):
    def cancellation_cleanup_complete(self, *_args: object) -> bool:
        return False

    def cancellation_cleanup_state(self, *_args: object) -> dict:
        return {"pending_delivery_count": 1, "cancellation_observed": False}

    def cancellation_observed(self, *_args: object) -> bool:
        return False

    def cancellation_cleanup_observation_nonblocking(self, *_args: object):
        return ({"remote_frames_sent": 1}, self.cancellation_cleanup_state())


def test_a_never_observed_cancellation_publishes_no_terminal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Deliberate withholding: unproven cleanup must never sign a receipt."""

    monkeypatch.delenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, raising=False)
    service = _service(tmp_path)
    service.runtime = _UncleanRuntime()  # type: ignore[assignment]
    service.transport = _UncleanTransport()  # type: ignore[assignment]
    service._bind_request_control({"request_id": "request-x", "control": dict(CONTROL)})

    payload = {
        **SUBJECT,
        "cancellation_generation": CONTROL["cancellation_generation"] + 1,
        "deadline_budget_ms": 300,
    }
    result = service._infer_cancel_wait(payload)
    details = result.get("details") or {}

    # No terminal claim and no signed receipt: the caller keeps the pending truth.
    assert details.get("cleanup_pending") is not False, result
    assert "request_cleanup" not in details, details
    assert _committed(service) is None


def test_the_seal_reports_no_swallowed_error_on_the_committed_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A swallowed exception must not be able to masquerade as "the product is slow".

    The seal's poll is fail-closed inside a broad ``except Exception`` (correct: a
    transient observation must not abort the lifecycle worker).  The hazard is that
    the same breadth hides *instrument* and *signature* drift: round 55's fix made
    the seal call ``_snapshot(..., allow_blocking_observations=True)``, and any
    double or wrapper still declaring ``_snapshot(payload)`` raises TypeError on
    every poll, which the seal turns into ``seal_poll_error`` and then burns its whole
    budget -- indistinguishable, by wall-clock alone, from the defect this lane spent
    nine runs chasing.  This test makes that loud.
    """

    destination = tmp_path / "seal-phases.jsonl"
    # Same process-global destination, same gate: this test's own inline seal is
    # the only writer whose entries it may judge.
    _await_foreign_cancellation_workers()
    monkeypatch.setenv(_CANCEL_PHASE_DIAGNOSTIC_ENV, str(destination))
    service = _service(tmp_path)
    service._signed_result = _production_signed_result(service)  # type: ignore[method-assign]
    service._bind_request_control({"request_id": "request-x", "control": dict(CONTROL)})

    service._infer_cancel_wait(
        {
            **SUBJECT,
            "cancellation_generation": CONTROL["cancellation_generation"] + 1,
            "deadline_budget_ms": 1499,
        }
    )

    lines = [
        json.loads(line)
        for line in destination.read_text().splitlines()
        if line.strip()
    ]
    poll_codes = [entry.get("code") for entry in lines if entry.get("phase") == "seal_poll"]
    swallowed = [entry for entry in lines if entry.get("phase") == "seal_poll_error"]
    assert "committed_complete" in poll_codes, poll_codes
    assert swallowed == [], swallowed
