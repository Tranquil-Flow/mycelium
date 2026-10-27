# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic target announcement consumer for the in-flight desk tests."""
import threading
from types import SimpleNamespace

from mycelium_a13_macos.demo_target import DemoTargetAgent
from physical_inference_qualification import ControllerError


def delayed_cleanup_target(stack, monkeypatch):
    """Delay real signed cancellation proof until the stop operation begins.

    The native worker double cannot answer commands once the target's real
    announcement consumer asks the injected app to stop its runtime. The pump,
    mailbox, owner, route and proof verification remain real. No synthetic
    cleanup result is returned for a stopped worker.
    """
    proof_may_return = threading.Event()
    runtime_stopped = threading.Event()
    native_send = stack.worker.send
    def send(**command):
        if command['command'] == 'infer_cancel_wait':
            assert proof_may_return.wait(3)
        if runtime_stopped.is_set():
            raise ControllerError('node_process_exited')
        return native_send(**command)
    monkeypatch.setattr(stack.worker, 'send', send)
    actions = []
    def action(path, body):
        assert (path, body) == ('/api/control/stop', {})
        actions.append(path)
        runtime_stopped.set()
    actor = SimpleNamespace(member=SimpleNamespace(node_id=stack.session.node_id),
        pending_cleanup=lambda: None,
        status=lambda: {'native_control': {'state': 'running'}})
    target = DemoTargetAgent(SimpleNamespace(authority=actor, action=action),
        consent=None, source=lambda: None, sink=lambda report: None)
    announcements = []
    def announce(**document):
        announcements.append(document['phase'])
        target._step(document)
    return SimpleNamespace(proof_may_return=proof_may_return, runtime_stopped=runtime_stopped,
        announcements=announcements, actions=actions, announce=announce)
