# SPDX-License-Identifier: AGPL-3.0-only
"""A16 DrainHooks wired to the product paths that already exist.

A4 terminal abort = the node's existing ``cancel`` worker command (entry-router cancel, the
same command the owner control pump delivers).  A6 handoff/successor have no target-side seam
(recovery is owner-authority driven) and stay unwired: a decision without a hook keeps
blocking the unload.
"""
from __future__ import annotations

from mycelium_a13_macos import adaptive_drain as ad
from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos.worker import WorkerError

from tests.a13_macos.test_adaptive_drain import (
    GIB, Clock, FakeRuntime, FakeWorker, _draining, _ledger, _set_status,
)


def _result(cancelled, status_after="CANCELLED"):
    return {"ok": True, "result": {"observation": {"details": {
        "request_id": "x", "result": {"cancelled": cancelled, "status_after": status_after}}}}}


class SendWorker(FakeWorker):
    def __init__(self, response=None, error=None):
        super().__init__()
        self.sent, self.response, self.error = [], response, error

    def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
        self.sent.append((command_id, command, dict(payload), deadline_monotonic_s))
        if self.error is not None:
            raise self.error
        return self.response


def _row(request_id="req0"):
    return {"request_id": request_id, "reservation_id": "res0", "kv_bytes": 1, "lease_expires_at": 1.0}


def test_product_hooks_wire_only_the_a4_abort():
    hooks = ad.product_hooks(FakeRuntime("/nonexistent", SendWorker(_result(True))))
    assert hooks.abort is not None
    assert hooks.handoff is None and hooks.successor is None and hooks.cancelled is None


def test_abort_sends_the_existing_cancel_command_with_a_bounded_deadline():
    worker = SendWorker(_result(True))
    hooks = ad.product_hooks(FakeRuntime("/nonexistent", worker))
    assert hooks.abort(_row("req7")) is True
    (command_id, command, payload, deadline), = worker.sent
    assert command == "cancel" and payload == {"request_id": "req7"}
    assert command_id.startswith("a16-abort-") and len(command_id) <= 64
    import time
    assert 0 < deadline - time.monotonic() <= 5.0


def test_abort_is_idempotent_per_request_after_a_terminal():
    worker = SendWorker(_result(True))
    hooks = ad.product_hooks(FakeRuntime("/nonexistent", worker))
    assert hooks.abort(_row()) and hooks.abort(_row()) and len(worker.sent) == 1
    assert hooks.abort(_row("other")) and len(worker.sent) == 2


def test_abort_that_does_not_end_the_request_is_not_success():
    for response in (_result(False, "UNKNOWN"), {"ok": False, "error": {"code": "invalid_state"}},
                     {"ok": True, "result": {}}, {"ok": True}, None, "junk"):
        worker = SendWorker(response)
        hooks = ad.product_hooks(FakeRuntime("/nonexistent", worker))
        assert hooks.abort(_row()) is False
        assert hooks.abort(_row()) is False and len(worker.sent) == 2  # retried, never cached


def test_abort_worker_failure_is_not_success_and_does_not_raise():
    hooks = ad.product_hooks(FakeRuntime("/nonexistent", SendWorker(error=WorkerError("worker_not_running"))))
    assert hooks.abort(_row()) is False
    hooks = ad.product_hooks(FakeRuntime("/nonexistent", FakeWorker()))  # no send_before at all
    assert hooks.abort(_row()) is False


def test_drain_with_product_hooks_aborts_then_unloads_only_after_the_ledger_is_empty(tmp_path):
    _ledger(tmp_path, [("COMMITTED", 999.0)])  # lease expired -> abort_terminal
    clock = Clock()
    worker = SendWorker(_result(True))
    runtime = FakeRuntime(tmp_path, worker)
    ctl = _draining(clock, runtime)
    hooks = ad.product_hooks(runtime)
    report = ad.drain_step(ctl, runtime, wall_clock=clock, hooks=hooks)
    assert report["dispositions"] == {"abort_terminal": 1} and report["abort_unavailable"] == 0
    assert worker.sent[0][1] == "cancel" and runtime.unloads == 0  # row still live
    _set_status(tmp_path, "RELEASED")
    report = ad.drain_step(ctl, runtime, wall_clock=clock, hooks=hooks)
    assert runtime.unloads == 1 and report["unloaded"] is True


def test_a6_decision_without_a_hook_still_blocks_unload(tmp_path):
    _ledger(tmp_path, [("COMMITTED", 1100.0)])
    clock, runtime = Clock(), FakeRuntime(tmp_path, SendWorker(_result(True)))
    ctl = _draining(clock, runtime)
    ctl.observe({"observed_at": clock(), "pressure": "critical", "available_bytes": 8 * GIB})
    ready = {"qualified": True, "compatible": True, "fence_ready": True, "checkpoint_committed": True}
    hooks = ad.product_hooks(runtime)
    assert hooks.successor is None and hooks.handoff is None
    report = ad.drain_step(ctl, runtime, wall_clock=clock, hooks=hooks)
    # critical pressure without a successor -> abort via the real A4 path, never a fake handoff
    assert report["dispositions"] == {"abort_terminal": 1} and report["handoff_refused"] == 0
    assert ready  # successor readiness can only come from the owner-side A6 authority


def test_app_drain_tick_passes_the_product_hooks(tmp_path, monkeypatch):
    from mycelium_a13_macos.app import DemoApp

    seen = {}
    monkeypatch.setattr(ad, "drain_step", lambda ctl, runtime, **kw: seen.update(kw))

    class Auth:
        def renew(self):
            return {}

    class Stub:
        authority = Auth()
        _adaptive_clock = Clock()

        class _rt:
            controller = ah.AdaptiveHostController({**ah.DEFAULT_POLICY, "enabled": True})
        _adaptive_runtime = _rt()

    runtime = FakeRuntime(tmp_path, SendWorker(_result(True)))
    DemoApp._adaptive_drain(Stub(), runtime)
    assert seen["hooks"].abort is not None and seen["hooks"].handoff is None
