# SPDX-License-Identifier: AGPL-3.0-only
"""A6 controlled source failure through the production composition on real processes.

Ordinary gateway -> LiveRouterPort (M16 coordinator, recovery authority, the
production ``LiveA6RecoveryQualifier``) -> ``ColdRouteRestartOwner`` -> real
``PhysicalLiveRoute`` over real local ``physical_inference_node.py`` processes.
The fault is the product seam itself (``ProductCaseSeam``): it SIGKILLs the
source node's owned process group exactly as the grant-gated fault control does.

Local multi-process session only; never physical or browser qualification.
"""
from __future__ import annotations

import faulthandler
import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from mycelium_a6_fault_control import EXACT_FLEET_GRANT
from mycelium_a6_m16 import M16RecoveryAuthority
from mycelium_a6_product_fault_seams import LiveProductFaultTarget, ProductCaseSeam
from mycelium_live.a6_recovery import LiveA6RecoveryQualifier
from mycelium_live.router_port import LiveRouterPort
from mycelium_live.supervisor import ColdRouteRestartOwner, _qualify_open_route
from mycelium_m16_runtime import build_live_m16_runtime
from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from tests.live._a6_local_physical import PROMPT, build_local_physical
from tests.physical_qualification.test_node_service import SIDECAR_BINARY

pytestmark = pytest.mark.skipif(
    not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built"
)

TOKENIZER_DIGEST = "sha256:" + "9" * 64
MAX_NEW = 8
_TERMINALS = {"completed", "failed", "cancelled"}


class _Codec:
    tokenizer_digest = TOKENIZER_DIGEST
    stop_token_ids = frozenset()

    def encode(self, prompt: str) -> tuple[int, ...]:
        return tuple(PROMPT)

    def decode_token(self, token_id: int) -> str:
        return f"<{token_id}>"


def _open_and_qualify(route, _generation):
    identity = route.open()
    qualification = _qualify_open_route(route)
    route.set_deployment_qualification(qualification, quantization="none")
    return identity, qualification


def _compose(tmp_path: Path, shape: str):
    local = build_local_physical(tmp_path, shape=shape)
    local.session_factory.step_delay_s = 0.15
    owner = ColdRouteRestartOwner(
        route_factory=local.new_route, open_and_qualify=_open_and_qualify, restart_budget=1)
    try:
        owner.open()
        return (local, owner, *_stack(tmp_path, owner))
    except BaseException:
        owner.close()
        owner.cleanup()
        local.close()
        raise


def _stack(tmp_path: Path, owner):
    graph = owner.execution_graph
    deployment_dir = tmp_path / "qualifier-deployment"
    deployment_dir.mkdir()
    (deployment_dir / "config.json").write_text("{}\n", encoding="utf-8")
    (deployment_dir / "tokenizer.json").write_text('{"version":"1.0"}\n', encoding="utf-8")
    coordinator = build_live_m16_runtime(graph)
    qualifier = LiveA6RecoveryQualifier(
        graph=graph, deployment_dir=deployment_dir, tokenizer_digest=TOKENIZER_DIGEST,
        route=owner)
    router = LiveRouterPort(
        route=owner, execution_graph=graph, runtime_coordinator=coordinator,
        recovery_authority=M16RecoveryAuthority(coordinator),
        recovery_qualification_source=qualifier,
        recovery_tokenizer_digest=TOKENIZER_DIGEST,
        recovery_a4_atomic_commit="a" * 40,
        qualification_source=owner,
    )
    owner.set_m16_runtime_source(router.runtime_status)
    (tmp_path / "checkpoints").mkdir(mode=0o700)
    service = RequestGatewayService(
        qualification_source=owner,
        backend=RouterSessionBackend(
            router=router, codec=_Codec(), clock=time.monotonic,
            qualification_source=owner),
        request_id_source=lambda: str(uuid.uuid4()),
        max_buffered_events=64,
        replay_checkpoint_root=tmp_path / "checkpoints",
    )
    target = LiveProductFaultTarget(
        router=router, route=owner, service=service, qualifier=qualifier,
        grant=EXACT_FLEET_GRANT)
    return router, service, target


def _submit(service, owner):
    return service.submit(InferenceSubmission(
        prompt="a6 local physical prompt", max_new_tokens=MAX_NEW,
        qualification=qualification_binding(owner.current())))


def _drain(service, request_id, events, first_token):
    subscription = service.subscribe(request_id, last_event_id=None)
    try:
        while True:
            event = subscription.next_event(timeout=120)
            if event is None:
                return
            events.append(event)
            subscription.ack(event.sequence)
            if event.kind == "token":
                first_token.set()
            if event.kind in _TERMINALS:
                return
    finally:
        first_token.set()
        subscription.close()


def _run_case(service, owner, target, case_id):
    request_id = _submit(service, owner)
    events: list = []
    first_token = threading.Event()
    reader = threading.Thread(
        target=_drain, args=(service, request_id, events, first_token), daemon=True)
    reader.start()
    assert first_token.wait(60), "request never produced a token"
    seam = ProductCaseSeam(case_id, target)
    outcome: dict = {}
    try:
        seam.arm(case_id=case_id)
        outcome["post_state"] = dict(seam.apply(case_id=case_id))
    except Exception as error:  # noqa: BLE001 - measured, asserted by caller
        outcome["refusal"] = str(error)
    reader.join(timeout=180)
    outcome["kinds"] = [event.kind for event in events if event.kind in _TERMINALS]
    outcome["events"] = [(event.kind, event.code) for event in events if event.kind != "token"]
    outcome["tokens"] = [event.text for event in events if event.kind == "token"]
    return request_id, outcome


def _run_without_fault(service, owner):
    request_id = _submit(service, owner)
    events: list = []
    _drain(service, request_id, events, threading.Event())
    return request_id, {
        "kinds": [event.kind for event in events if event.kind in _TERMINALS],
        "events": [(event.kind, event.code) for event in events if event.kind != "token"],
        "tokens": [event.text for event in events if event.kind == "token"],
    }


def _wait_ready(owner, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = owner.public_status()
        if status["cold_route_restart"]["state"] == "failed":
            break
        if status["cold_route_restart"]["state"] == "ready" and status["route_ready"]:
            break
        time.sleep(0.1)
    return owner.public_status()["cold_route_restart"]


_ROUTE_THREAD_PREFIXES = ("m16-dispatch-", "live-route-worker-")


@pytest.fixture(autouse=True)
def no_route_threads_survive():
    """Every LiveRouterPort the test composes must be closed with the test.

    A surviving dispatch loop or route worker keeps running in the xdist
    worker and opens descriptors asynchronously, racing process-wide fd
    inventory tests in the same worker.
    """

    before = {thread.ident for thread in threading.enumerate()}
    yield
    survivors = sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.ident not in before
        and thread.is_alive()
        and thread.name.startswith(_ROUTE_THREAD_PREFIXES)
    )
    assert survivors == [], survivors


@pytest.fixture
def watchdog():
    faulthandler.dump_traceback_later(300, exit=False, file=sys.stderr)
    yield
    faulthandler.cancel_dump_traceback_later()


def test_single_path_source_kill_is_observed_then_route_cold_restarts(
        tmp_path, watchdog, caplog) -> None:
    """Run-day topology: no disjoint successor exists.

    The source failure is observed even though the refused recovery settles
    and the gateway releases the request first; the released degraded route
    is cold-restarted, and the replacement serves the next ordinary request
    (the router follows the replacement's qualification).
    """

    local, owner, router, service, target = _compose(tmp_path, "single")
    try:
        first_route = owner._current
        request_id, outcome = _run_case(
            service, owner, target, "ordinary_browser_source_failure_recovery")
        assert "refusal" not in outcome, outcome
        assert outcome["post_state"]["source_failure_observed"] is True
        assert outcome["post_state"]["actual_incident"]["subject_id"] == "node-a"
        assert outcome["kinds"] == ["failed"], outcome
        assert router.recovery_failure_reason(request_id) == "recovery_successor_unavailable"
        # The refusal reason is the failed terminal's code and is logged.
        assert ("failed", "recovery_successor_unavailable") in outcome["events"], outcome
        logged = [json.loads(record.getMessage()) for record in caplog.records
                  if record.name == "mycelium_live.router_port"
                  and record.levelno == logging.WARNING
                  and record.getMessage().startswith('{"error_code"')]
        assert any(document["event"] == "a6_recovery_failed"
                   and document["request_id"] == request_id
                   and document["reason"] == "recovery_successor_unavailable"
                   for document in logged), logged
        status = _wait_ready(owner, 120)
        assert status["state"] == "ready", status
        assert status["generation"] == 2
        assert owner._current is not first_route
        _second, second = _run_without_fault(service, owner)
        assert second["kinds"] == ["completed"], second
        assert local.session_factory.launches == ["node-a", "node-b", "node-a", "node-b"]
    finally:
        service.close()
        try:
            router.close()
        finally:
            owner.close()
            local.close()


def test_full_replica_source_kill_recovers_on_the_disjoint_successor(tmp_path, watchdog) -> None:
    """Two complete disjoint paths: the request continues on the successor.

    The source (whole model on node-a) is SIGKILLed by the product seam after
    a committed prefix; M16 reserves and the production qualifier physically
    challenges the disjoint successor (whole model on node-b), replays the
    exact context, cuts over, and the ordinary gateway stream completes with
    the uninterrupted token sequence. The degraded route is then replaced.
    """

    local, owner, router, service, target = _compose(tmp_path, "full_replica")
    try:
        _baseline_id, baseline = _run_without_fault(service, owner)
        assert baseline["kinds"] == ["completed"], baseline
        request_id, outcome = _run_case(
            service, owner, target, "ordinary_browser_source_failure_recovery")
        assert "refusal" not in outcome, outcome
        assert outcome["post_state"]["source_failure_observed"] is True
        assert outcome["post_state"]["recovery_required"] is True
        assert outcome["post_state"]["actual_incident"]["subject_id"] == "node-a"
        assert outcome["kinds"] == ["completed"], outcome
        assert outcome["tokens"] == baseline["tokens"]
        issued = target.qualifier.issued_qualification_facts(request_id)
        assert issued["successor_placement_ids"] == ["placement-001"]
        assert issued["physical_challenge"] == "passed"
        challenge = target.qualifier.physical_challenge_outcome(request_id)
        assert challenge["outcome"] == "passed", challenge
        assert router.recovery_failure_reason(request_id) is None
        status = _wait_ready(owner, 120)
        assert status["state"] == "ready" and status["generation"] == 2, status
        _after_id, after = _run_without_fault(service, owner)
        assert after["kinds"] == ["completed"], after
    finally:
        service.close()
        try:
            router.close()
        finally:
            owner.close()
            local.close()


def test_full_replica_recovery_after_committed_prefix_reaches_cutover_inside_default_phases(
        tmp_path, watchdog) -> None:
    """A6 live a6mbd12 shape: the source fails after >= 1 committed token.

    The gateway freezes a checkpoint that carries the committed prefix, the
    disjoint complete replica is qualified and prefills the replayed context
    (prompt + committed prefix), the suffix cuts over, and the request completes
    on the default (unwidened) phase deadlines. Observed through the production
    ``recover_full_context`` seam; no sleeps are added and no deadline is changed.
    """

    local, owner, router, service, target = _compose(tmp_path, "full_replica")
    try:
        _baseline_id, baseline = _run_without_fault(service, owner)
        assert baseline["kinds"] == ["completed"], baseline
        seen: dict = {}
        recover_returned = threading.Event()
        real_recover = router.recover_full_context

        def observed_recover(request_id, checkpoint):
            seen["committed_token_count"] = checkpoint.committed_token_count
            seen["prefix"] = tuple(checkpoint.committed_prefix_tokens)
            seen["prompt_len"] = len(checkpoint.encoded_prompt)
            try:
                seen["result"] = real_recover(request_id, checkpoint)
            except BaseException as error:  # noqa: BLE001 - recorded, re-raised
                seen["error"] = repr(error)
                raise
            finally:
                recover_returned.set()
            return seen["result"]

        router.recover_full_context = observed_recover
        request_id, outcome = _run_case(
            service, owner, target, "ordinary_browser_source_failure_recovery")
        assert "refusal" not in outcome, outcome
        assert seen.get("committed_token_count", 0) >= 1, seen
        assert seen["prefix"] and len(seen["prefix"]) == seen["committed_token_count"], seen
        assert outcome["kinds"] == ["completed"], (outcome, seen)
        # The terminal event is published before recover_full_context returns.
        assert recover_returned.wait(60), (seen, outcome)
        assert seen["result"] is True, seen
        assert outcome["kinds"] == ["completed"], outcome
        assert outcome["tokens"] == baseline["tokens"], outcome
        assert router.recovery_failure_reason(request_id) is None
    finally:
        service.close()
        try:
            router.close()
        finally:
            owner.close()
            local.close()
