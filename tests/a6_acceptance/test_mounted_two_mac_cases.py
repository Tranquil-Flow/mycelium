# SPDX-License-Identifier: AGPL-3.0-only
"""CPU controls of the actual supervisor-mounted A6 receiver, not physical proof."""
from __future__ import annotations

import signal
import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_a6_fault_control import EXACT_FLEET_GRANT, FaultRejected
from mycelium_a6_live_fault_seams import MeasuredSourceFailureSeam
from mycelium_a6_product_fault_control import GRANT_ENVIRONMENT
from mycelium_live import supervisor
from tests.a6_acceptance.test_product_fault_control import _post
from tests.a6_acceptance.a6_measured_stack import build_measured_stack, settle
from tests.a6_acceptance.test_product_fault_seams import _RealPeer, _KillableSession
from tests.a6_acceptance.test_product_fault_seams_cases import (
    _Backend, _Router, _owned_service, _target, REQUEST_ID,
)


def test_mounted_no_successor_on_real_gateway_router_and_coordinator(monkeypatch, tmp_path):
    """Withholding happens before a real source peer dies, not in a view fake."""
    with build_measured_stack(monkeypatch, tmp_path) as stack:
        source_node = stack.binding['source_node_id']
        peer = _RealPeer(on_signal=lambda signum: (
            peer.child.wait(timeout=5), stack.hold.set()) if signum == signal.SIGKILL else None)
        stack.route._sessions[source_node] = _KillableSession(peer)
        app = SimpleNamespace(_request_gateway_app=SimpleNamespace(_service=stack.service))
        live = SimpleNamespace(router=stack.router, app=app)
        monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
        try:
            mount = supervisor._mount_a6_product_fault_control(
                stack=live, route=stack.route, product_port=8787, control_port=0,
                public_origin=None, trusted_https_proxy=False, request_stop=lambda: None)
            try:
                assert _post(mount.port, case_id='no_successor', grant=None)[0] == 403
                assert stack.router.a6_source_failure_observed(stack.binding) is False
                assert _post(mount.port, case_id='no_successor')[0] == 200
                assert _post(mount.port, case_id='no_successor')[0] == 409
                assert mount.executed_case_ids == ('no_successor',)
                assert settle(stack)
                session = stack.service._get_session(stack.request_id)
                assert session.terminal_event is not None
                assert session.terminal_event.kind == 'failed'
                assert stack.service.terminal_event_count(stack.request_id) == 1
                assert stack.router.a6_request_view(stack.request_id)['recovery_failure_reason'] == 'recovery_successor_unavailable'
                assert next(item for item in stack.coordinator.status()['requests']
                            if item['request_id'] == stack.request_id)['reservation_count'] == 0
                events = tuple(session.events)
                assert sum(e.kind == 'token' for e in events) == 0
                assert tuple(session.events) == events
            finally:
                mount.close()
        finally:
            stack.hold.set()
            peer.close()


@pytest.mark.parametrize("case", ["no_successor", "cancellation_during_prefill"])
def test_inprocess_positive_only_seam_rejects_negative_case(case):
    seam = object.__new__(MeasuredSourceFailureSeam)
    seam._used = False
    with pytest.raises(FaultRejected, match="measured_fault_case_unavailable"):
        seam.arm(case_id=case)
    with pytest.raises(FaultRejected, match="measured_fault_not_armed"):
        seam.apply(case_id=case)


@pytest.mark.parametrize("case", ["no_successor", "cancellation_during_prefill"])
def test_supervisor_mount_executes_negative_with_grant_and_cleans_up(monkeypatch, case):
    router = _Router()
    reservation = {"count": 1}
    router._coordinator.status = lambda: {"requests": [
        {"request_id": REQUEST_ID, "reservation_count": reservation["count"]}]}
    signals = []
    stopped = threading.Event()
    resumed = threading.Event()

    class Backend(_Backend):
        def run(self, request_id, submission, emit_token, is_cancelled):
            self.late_emit = emit_token
            emit_token(0, "first")
            self.started.set()
            self.release.wait(timeout=10)
            reservation["count"] = 0
            # Late source/successor frames are intentionally attempted only
            # after terminal in this double; gateway rejection is checked below.
            return "cancelled" if self.cancelled else "failed"

    def on_cancel():
        router.state.update(cancellation_linearized=True, cancellation_requested=True,
                            recovery_phase="recovery_prefill")
        assert resumed.wait(timeout=5)

    backend = Backend(on_cancel)
    service = _owned_service(backend)

    def on_signal(node_id, signum):
        signals.append((node_id, signum))
        if node_id == "node-0" and signum == signal.SIGKILL:
            if case == "no_successor":
                assert router.withheld is True
                router.state.update(recovery_phase="cleanup", recovery_attempts=0,
                                    recovery_failure_reason="recovery_successor_unavailable",
                                    terminal_status="FAILED")
            else:
                router.state.update(recovery_phase="recovery_prefill", recovery_attempts=1)
        elif node_id == "node-2" and signum == signal.SIGSTOP:
            stopped.set()
        elif node_id == "node-2" and signum == signal.SIGCONT:
            resumed.set()

    router.withheld = False
    router.a6_withhold_recovery_successors = lambda _rid: setattr(router, "withheld", True) or True
    target, _ = _target(router, service, on_signal)
    router._recovery_qualification_source = target.qualifier
    stack = SimpleNamespace(router=router, app=SimpleNamespace(
        _request_gateway_app=SimpleNamespace(_service=service)))
    monkeypatch.delenv(GRANT_ENVIRONMENT, raising=False)
    try:
        with pytest.raises(RuntimeError, match="explicit_fleet_grant_required"):
            supervisor._mount_a6_product_fault_control(
                stack=stack, route=target.route, product_port=8787, control_port=0,
                public_origin=None, trusted_https_proxy=False, request_stop=lambda: None)
        assert not signals and not router.withheld and not backend.cancelled
        monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
        mount = supervisor._mount_a6_product_fault_control(
            stack=stack, route=target.route, product_port=8787, control_port=0,
            public_origin=None, trusted_https_proxy=False, request_stop=lambda: None)
        try:
            assert _post(mount.port, case_id=case, grant=None)[0] == 403
            assert _post(mount.port, case_id=case, grant="wrong")[0] == 403
            assert not signals and not router.withheld and not backend.cancelled
            assert _post(mount.port, case_id=case)[0] == 200
            assert _post(mount.port, case_id=case)[0] == 409
            assert mount.executed_case_ids == (case,)
            # The mount creates its own target; the in-test target is used only
            # to supply the route double and never drives the receiver.
            assert target.executed == ()
            backend.release.set()
            until = time.monotonic() + 5
            while service.terminal_event_count(REQUEST_ID) != 1 and time.monotonic() < until:
                time.sleep(.005)
            assert service.terminal_event_count(REQUEST_ID) == 1
            session = service._get_session(REQUEST_ID)
            assert session.terminal_event.kind == ("cancelled" if case.startswith("cancellation") else "failed")
            assert reservation["count"] == 0
            assert not router._coordinator.status()["requests"][0]["reservation_count"]
            if case == "no_successor":
                assert router.withheld and not stopped.is_set()
                assert signals == [("node-0", signal.SIGKILL)]
            else:
                assert stopped.is_set() and resumed.is_set()
                assert backend.cancelled == [REQUEST_ID]
                assert signals == [("node-0", signal.SIGKILL),
                                   ("node-2", signal.SIGSTOP), ("node-2", signal.SIGCONT)]
            # A real gateway session is terminal. No post-terminal events from
            # this controlled double; physical successor-stop is still open.
            before = tuple(session.events)
            with pytest.raises(Exception):
                backend.late_emit(1, "forbidden late successor")
            assert tuple(session.events) == before
            assert service.terminal_event_count(REQUEST_ID) == 1
        finally:
            mount.close()
    finally:
        backend.release.set()
        service.close()
