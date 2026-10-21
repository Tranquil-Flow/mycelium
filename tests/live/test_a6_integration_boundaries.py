# SPDX-License-Identifier: AGPL-3.0-only
"""Local A6 integration regressions against current A4/A5 ownership seams."""
import threading

from tests.live.test_a6_live_router_recovery import (
    FailingThenRecoveringRoute, _runtime, _submit_and_drain,
)


class CallbackCleanupRoute(FailingThenRecoveringRoute):
    """Synthetic exact receipts at the production callback seam; no fleet."""
    def infer(self, *args, **kwargs):
        try:
            return super().infer(*args, **kwargs)
        finally:
            callback = kwargs.get("publish_cleanup_receipt")
            assert callable(callback), "successor_cleanup_callback_missing"
            command = kwargs["command_identity"]
            receipt = {
                **command,
                "cleanup_owner_id": "physical-live-route:" + command["deployment_id"],
                "node_ids": [],
            }
            callback(receipt)
            if command["request_attempt"] == 1:
                self.old_cleanup_callback = callback
                self.old_cleanup_receipt = receipt
            else:
                # Late source completion must not poison successor cleanup.
                self.old_cleanup_callback(self.old_cleanup_receipt)


def test_successor_cleanup_uses_its_own_receipt_callback(tmp_path):
    route = CallbackCleanupRoute()
    coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [e.kind for e in events if e.kind in {"completed", "failed", "cancelled"}] == ["completed"]
        assert [e.token_index for e in events if e.kind == "token"] == [0, 1, 2, 3]
        assert coordinator.terminal_state(request_id) == "completed"
    finally:
        service.close()


def test_successor_obeys_controller_failed_terminal(tmp_path, monkeypatch):
    coordinator, router, service, qualification = _runtime(tmp_path, FailingThenRecoveringRoute())
    original = router._record_command_terminal
    def controller_terminal(pending, terminal, **kwargs):
        # The current adapter may return FAILED after a deadline wins the CAS.
        return original(pending, "FAILED" if terminal == "COMPLETED" else terminal, **kwargs)
    monkeypatch.setattr(router, "_record_command_terminal", controller_terminal)
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [e.kind for e in events if e.kind in {"completed", "failed", "cancelled"}] == ["failed"]
        assert coordinator.terminal_state(request_id) == "failed"
    finally:
        service.close()


def test_decode_wait_returns_when_recovery_becomes_required(tmp_path):
    from mycelium_live.router_port import _Pending
    coordinator, router, service, qualification = _runtime(tmp_path, FailingThenRecoveringRoute())
    pending = _Pending()
    done = threading.Event()
    result = []
    def decode():
        result.append(router.decode_one("waiting"))
        done.set()
    with router._changed:
        router._pending["waiting"] = pending
    worker = threading.Thread(target=decode)
    worker.start()
    try:
        with router._changed:
            pending.recovery_required = True
            router._changed.notify_all()
        assert done.wait(1.0), "decode wait hid recovery transition"
        assert result == [False]
    finally:
        with router._changed:
            pending.terminal_status = "FAILED"
            router._changed.notify_all()
        worker.join(2)
        with router._changed:
            router._pending.pop("waiting")
        service.close()
