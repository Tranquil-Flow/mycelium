# SPDX-License-Identifier: AGPL-3.0-only
"""The A7 physical measurement plug-in over two real local node processes.

These tests drive the plug-in's own measurement (``measure``) against a real
``PhysicalLiveRoute`` that opens two real ``physical_inference_node.py``
processes over native iroh (the R7 harness of ``test_a7_product_path``), read
the evidence through the product's grant-gated A7 evidence door over loopback
HTTP, and kill the stage-1 source's process group with the plug-in's own fault
target (discovered by command-line pattern, exactly as on the fleet).

The request is driven through the route, not through the product's browser
gateway, so the driver honestly reports ``ordinary_browser_request = False``
and the product evidence stream has no A7 workspace entity: the gate must
therefore reject these records on exactly those predicates (plus, for the
fallback harness, the predicates the missing A6 replay cannot satisfy), while
every A7 predicate the plug-in measured is judged on real observations.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mycelium_a7_evidence_control import (
    EXACT_FLEET_GRANT,
    A7EvidenceControl,
    RouteA7Evidence,
)
from mycelium_a7_physical_measurement import (
    HttpEvidence,
    MeasurementRefused,
    MeasurementSettings,
    RunningRequest,
    SourceProcessGroupTarget,
    measure,
    measure_case,
)
from tests.live import test_a7_product_path as harness
from tests.physical_qualification.test_node_service import NODE_SCRIPT, SIDECAR_BINARY

ROOT = Path(__file__).resolve().parents[2]
_GATE_SPEC = importlib.util.spec_from_file_location("a7_gate_for_measurement", ROOT / "scripts" / "run_a7_kv_successor_gate.py")
assert _GATE_SPEC is not None and _GATE_SPEC.loader is not None
gate = importlib.util.module_from_spec(_GATE_SPEC)
_GATE_SPEC.loader.exec_module(gate)
_RUNNER_SPEC = importlib.util.spec_from_file_location("a7_runner_for_measurement", ROOT / "scripts" / "run_a7_kv_successor_case_runner.py")
assert _RUNNER_SPEC is not None and _RUNNER_SPEC.loader is not None
runner = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(runner)

CANDIDATE = "b" * 40
_MAX_NEW = 12  # the local deployment has n_positions=16 (prompt 3 + 12 - 1 < 16)
_HARNESS_ONLY = {"not_an_ordinary_browser_request", "workspaces"}

needs_sidecar = pytest.mark.skipif(not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built")


class _DecodeHold:
    """Hold the measured request's next decode step until the measurement's kill lands.

    ``PhysicalLiveRoute.infer`` emits each token to its sink before it sends the
    next ``infer_decode``, so blocking the sink after ``after`` tokens leaves the
    request exactly at that watermark.  On the ~ms local model the measurement's
    freeze (evidence reads, SIGSTOP, quiescence reads) is slower than one decode
    step, so without the hold a quiet host ends the request before the fault
    whenever few steps are killable (only the final step at 11 of 12).  The hold
    is released by the kill (``_HoldReleasingTarget``), by the harness when the
    measurement returns without one, or by its own bound; ``released_by`` says
    which.
    """

    def __init__(self, after: int, *, timeout_s: float = 120.0) -> None:
        self.after = after
        self.timeout_s = timeout_s
        self.engaged = threading.Event()
        self.released = threading.Event()
        self.released_by: str | None = None
        self.tokens_at_hold: int | None = None
        self._lock = threading.Lock()

    def hold(self, tokens_seen: int) -> None:
        if self.engaged.is_set():
            return  # one hold per measured request
        self.tokens_at_hold = tokens_seen
        self.engaged.set()
        if not self.released.wait(self.timeout_s):
            self.release("timeout")

    def release(self, by: str) -> None:
        with self._lock:
            if self.released_by is None:
                self.released_by = by
        self.released.set()


@dataclass(frozen=True)
class _HoldReleasingTarget(SourceProcessGroupTarget):
    """The plug-in's own fault target; its kill releases the harness decode hold."""

    hold: Any = field(default=None, compare=False, repr=False)

    def kill(self, group: Any) -> Any:
        try:
            return super().kill(group)
        finally:
            if self.hold is not None:
                self.hold.release("kill")


class _RouteDriver:
    """Drive ``PhysicalLiveRoute.infer`` (not the browser gateway) into a RunningRequest.

    With ``hold`` the request labelled ``measured`` is held after ``hold.after``
    tokens (see ``_DecodeHold``); the baseline request is never held.
    """

    ordinary_browser_request = False

    def __init__(self, state: Any, *, hold: _DecodeHold | None = None) -> None:
        self._state = state
        self._hold = hold
        self.request_ids: list[str] = []

    def start(self, label: str) -> RunningRequest:
        running = RunningRequest(label=label)
        request_id = f"a7m{label[:4]}" + uuid.uuid4().hex[:20]
        running.request_id = request_id
        self.request_ids.append(request_id)
        manifest, route_identity, command_identity = harness._product_identity(self._state.graph, request_id)
        hold = self._hold if label == "measured" else None
        running.decode_hold = hold  # type: ignore[attr-defined]

        class _Sink:
            def emit(self, token_index: int, token_id: int) -> None:
                running.on_token(token_index, token_id, publisher_generation=command_identity["publisher_generation"])
                if hold is not None and token_index == hold.after - 1:
                    hold.hold(len(running.snapshot()[0]))

        def run() -> None:
            route = self._state.route
            try:
                route.infer(
                    harness._PROMPT,
                    max_new_tokens=harness._MAX_NEW,
                    request_id=request_id,
                    sink=_Sink(),
                    route_identity=route_identity,
                    locked_path_manifest=manifest,
                    command_identity=command_identity,
                    authorize_cleanup=lambda deadline: deadline,
                    publish_cleanup_receipt=lambda _receipt: None,
                )
                running.on_terminal("completed")
            except Exception:  # noqa: BLE001 - the terminal is the observation
                running.on_terminal("failed")
            finally:
                # The gateway releases the route's request state after a terminal.
                route.release_request(request_id)
                running.finish()

        threading.Thread(target=run, name=f"a7-measure-{label}", daemon=True).start()
        return running

    def workspaces_checked(self) -> list[str]:
        return []


def _token_file(tmp_path: Path) -> Path:
    """A private per-test token file for the door under test."""
    directory = tmp_path / "a7-evidence-token"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / "a7-evidence.token"


def _source_target(state: Any, *, hold: _DecodeHold | None = None) -> SourceProcessGroupTarget:
    run_id = state.route._plan["run_id"]
    return _HoldReleasingTarget(
        transport="local",
        process_pattern=re.escape(str(NODE_SCRIPT)) + r" .*--run-id " + re.escape(run_id) + r" .*--node-id " + re.escape(harness._SOURCE_NODE_ID) + r"(\s|$)",
        hold=hold,
    )


def assert_hold_fired(hold: _DecodeHold, observations: dict[str, Any], kill_after_committed: int) -> None:
    """The kill landed while the measured request was held at the configured watermark."""

    assert hold.engaged.is_set(), "the measured request was never held"
    assert hold.released_by == "kill", hold.released_by
    assert hold.tokens_at_hold == kill_after_committed
    assert observations["fault"]["tokens_received_at_kill"] == kill_after_committed


def _measure_with_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, qualification_expired: bool, kill_after_committed: int = 2) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any]:
    # Measured once, never retried: the request is held after
    # ``kill_after_committed`` tokens until the kill lands (``_DecodeHold``), so
    # a7_request_ended_before_fault is a real failure here, not host timing.
    # Long enough that a final decode step is distinct from the mid-request kill.
    monkeypatch.setattr(harness, "_MAX_NEW", _MAX_NEW)
    state = harness._build_r7_staged_route(tmp_path, monkeypatch, qualification_expired=qualification_expired)
    control = None
    hold = _DecodeHold(kill_after_committed)
    try:
        harness._open_with_a7_authority(state)
        state.route._deployment_qualification = type("Qualified", (), {"route_ready": True})()
        control = A7EvidenceControl(grant=EXACT_FLEET_GRANT, source=RouteA7Evidence(state.route), token_file=_token_file(tmp_path), product_port=8787, port=0)
        control.start()
        evidence = HttpEvidence(f"http://127.0.0.1:{control.port}/a7/evidence", grant=EXACT_FLEET_GRANT, token_file=control.token_file)
        fields, observations = measure(
            driver=_RouteDriver(state, hold=hold),
            evidence=evidence,
            fault=_source_target(state, hold=hold),
            settings=MeasurementSettings(kill_after_committed=kill_after_committed, settle_timeout_s=10.0, result_timeout_s=20.0, terminal_timeout_s=120.0),
        )
        assert_hold_fired(hold, observations, kill_after_committed)
        plan = gate.build_plan(CANDIDATE)
        case = gate.INCOMPATIBLE if qualification_expired else gate.POSITIVE
        record = runner._measured_record(plan, case, fields)
        scoped = harness._scoped_node_exit_incidents(state.route, observations_request_id(state))
        observations["_product_evidence"] = RouteA7Evidence(state.route).product()
        return record, observations, plan, scoped
    finally:
        # A measurement that refused before its kill must not leave the request held.
        hold.release("measurement_returned")
        if control is not None:
            control.close()
        state.close()


def observations_request_id(state: Any) -> str:
    # The measured request is the most recently registered A7 request on the route.
    incidents = [item for item in state.route.a4_scoped_runtime_incidents() if item.get("subject_id") == harness._SOURCE_NODE_ID]
    return incidents[-1]["request_id"] if incidents else ""


@needs_sidecar
def test_plug_in_measures_the_positive_cutover_from_product_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record, observations, plan, scoped = _measure_with_harness(tmp_path, monkeypatch, qualification_expired=False)

    # Structurally a closed, plan-bound case record the gate judges.
    assert set(record) == set(gate._CASE_FIELDS)
    failures = gate.evaluate_case(record, plan)
    assert "not_an_ordinary_browser_request" in failures
    assert "workspaces" in failures
    # Every A7 predicate measured here passes; only the harness limits (and, on
    # this tiny local model, possibly the TPOT budget, whose base is a ~ms
    # decode) remain.
    assert set(failures) - _HARNESS_ONLY <= {"added_tpot"}, failures
    print(json.dumps({"positive_failures": failures, "interruption_ms": record["interruption_ms"], "cutover_ms": record["cutover_ms"], "baseline_tpot_ms": record["baseline_tpot_ms"], "candidate_tpot_ms": record["candidate_tpot_ms"], "detection_latency_ms": observations["detection_latency_ms"], "checkpoint_count": record["checkpoint_count"], "checkpoint_bytes_total": record["checkpoint_bytes_total"]}, sort_keys=True))

    assert record["fault"] == "source_process_kill"
    assert record["outcome"] == "fenced_kv_successor"
    assert record["kv_continuity_claimed"] is True
    assert record["committed_before_failure"] >= 2
    assert record["acknowledged_before_failure"] == record["committed_before_failure"]
    assert record["successor_recovery_prefill_operations"] == 0
    assert record["prefix_tokens_sent_to_successor"] == 0
    assert record["successor_decode_operations"] >= 1
    assert record["reference_parity"] is True
    assert record["duplicate_tokens"] == record["missing_tokens"] == 0
    assert record["publishers"] == record["terminal_results"] == 1
    assert record["checkpoint_count"] >= record["committed_before_failure"]
    assert record["checkpoint_bytes_total"] > 0
    assert record["interruption_ms"] <= gate.THRESHOLDS["max_interruption_ms"]
    assert record["cleanup"] == {
        harness._ENTRY_NODE_ID: {name: 0 for name in gate._CLEANUP_FIELDS}
    }
    assert observations["fault"]["group_gone"] is True
    assert observations["fault"]["kill_condition"] == "acknowledged_equals_committed"
    assert observations["detection_latency_ms"] is not None
    assert -50 <= observations["detection_latency_ms"] < 1_000
    assert len(scoped) == 1


@needs_sidecar
def test_plug_in_measures_a_final_decode_step_cutover_from_product_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The source is killed with only the final decode step left (11 of 12 committed)."""

    record, observations, plan, scoped = _measure_with_harness(tmp_path, monkeypatch, qualification_expired=False, kill_after_committed=_MAX_NEW - 1)
    failures = gate.evaluate_case(record, plan)
    assert set(failures) - _HARNESS_ONLY <= {"added_tpot"}, failures
    assert record["outcome"] == "fenced_kv_successor", record
    assert record["kv_continuity_claimed"] is True
    assert record["committed_before_failure"] == record["acknowledged_before_failure"] == _MAX_NEW - 1
    assert record["successor_recovery_prefill_operations"] == 0
    assert record["prefix_tokens_sent_to_successor"] == 0
    assert record["successor_decode_operations"] == 1
    assert record["reference_parity"] is True
    assert record["duplicate_tokens"] == record["missing_tokens"] == 0
    assert record["publishers"] == record["terminal_results"] == 1
    assert record["cleanup"] == {
        harness._ENTRY_NODE_ID: {name: 0 for name in gate._CLEANUP_FIELDS}
    }
    assert observations["fault"]["kill_condition"] == "acknowledged_equals_committed"
    assert len(scoped) == 1


def _install_node_outruns_measurement(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Make the local node always faster than the measurement's freeze.

    The measurement freezes the source only after an unfrozen settle read
    (``_await_unfrozen_settle``).  This seam holds that read until the measured
    request has ended, which is what a quiet host does on the ~ms local model
    when only one decode step is killable, unless the harness holds the
    request's next decode step until the kill lands (``decode_hold`` engaged).
    Bounded, so a hang still fails instead of wedging the test.
    """

    import mycelium_a7_physical_measurement as measurement

    real = measurement._await_unfrozen_settle
    seen: dict[str, Any] = {"calls": 0, "request_done": 0, "decode_held": 0}

    def outrun(evidence: Any, request_id: str, running: RunningRequest, settings: MeasurementSettings) -> None:
        seen["calls"] += 1
        hold = getattr(running, "decode_hold", None)
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if running.done.is_set():
                seen["request_done"] += 1
                break
            if hold is not None and hold.engaged.is_set():
                seen["decode_held"] += 1
                break
            time.sleep(0.01)
        real(evidence, request_id, running, settings)

    monkeypatch.setattr(measurement, "_await_unfrozen_settle", outrun)
    return seen


@needs_sidecar
@pytest.mark.parametrize("kill_after_committed", [pytest.param(2, id="mid_request"), pytest.param(_MAX_NEW - 1, id="final_decode_step")])
def test_harness_measurement_kills_before_the_request_ends_when_the_node_outruns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kill_after_committed: int
) -> None:
    """A node faster than the freeze must not end the measured request before the kill."""

    seen = _install_node_outruns_measurement(monkeypatch)
    record, observations, _plan, scoped = _measure_with_harness(
        tmp_path, monkeypatch, qualification_expired=False, kill_after_committed=kill_after_committed
    )
    assert seen["calls"] >= 1 and seen["request_done"] == 0 and seen["decode_held"] == seen["calls"], seen
    assert record["outcome"] == "fenced_kv_successor", record
    assert record["committed_before_failure"] == record["acknowledged_before_failure"] == kill_after_committed
    assert observations["fault"]["tokens_received_at_kill"] == kill_after_committed
    assert record["successor_decode_operations"] == _MAX_NEW - kill_after_committed
    assert record["reference_parity"] is True
    assert record["duplicate_tokens"] == record["missing_tokens"] == 0
    assert len(scoped) == 1


@needs_sidecar
def test_plug_in_measures_the_truthful_fallback_from_product_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record, observations, plan, scoped = _measure_with_harness(tmp_path, monkeypatch, qualification_expired=True)

    assert set(record) == set(gate._CASE_FIELDS)
    assert record["fault"] == "source_process_kill"
    assert record["outcome"] == "full_context_replay"
    assert record["reason"] == "qualification_not_current"
    assert record["kv_continuity_claimed"] is False
    assert record["successor_decode_operations"] == 0
    assert record["successor_recovery_prefill_operations"] == 0
    assert record["committed_before_failure"] >= 2
    assert record["terminal_results"] == 1
    assert record["duplicate_tokens"] == 0
    assert record["stale_source_publications_accepted"] == 0
    # The A6-handed fallback releases the entry's durable fence record and
    # shadow at its terminal (G4): every terminal counter returns to baseline.
    assert record["cleanup"] == {
        harness._ENTRY_NODE_ID: {name: 0 for name in gate._CLEANUP_FIELDS}
    }
    assert observations["a7_result"]["outcome"] == "full_context_replay"
    assert observations["measured"]["terminals"] == ["failed"]
    assert len(scoped) == 1 and scoped[0]["reason"] == "node_process_exited"
    failures = gate.evaluate_case(record, plan)
    # The gate judges the fallback cases' A7 decision only (FALLBACK_SCOPE), so
    # the missing A6 output (this harness has no product router port) is not a
    # failure. This harness falls back on an expired qualification, which is
    # not the incompatible case's reason, so the gate rejects the reason.
    assert set(failures) == _HARNESS_ONLY | {"fallback_reason"}, failures


def _plan_file(tmp_path: Path) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(gate.build_plan(CANDIDATE), sort_keys=True), encoding="utf-8")
    return path


def _run_case_runner(tmp_path: Path, env_extra: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], Path]:
    record_out = tmp_path / f"{gate.POSITIVE}.json"
    env = {key: value for key, value in os.environ.items() if not key.startswith("MYCELIUM_A7_")}
    env.update(env_extra, PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts" / "run_a7_kv_successor_case_runner.py"), "--case", gate.POSITIVE, "--plan", str(_plan_file(tmp_path)), "--record-out", str(record_out)],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )
    return completed, record_out


def test_case_runner_without_grant_never_reaches_the_plug_in(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    completed, record_out = _run_case_runner(tmp_path, {"MYCELIUM_A7_PHYSICAL_CONFIG": str(config)})
    assert completed.returncode == 0, completed.stderr
    record = json.loads(record_out.read_text(encoding="utf-8"))
    assert record["reason"] == "fleet_grant_missing"
    assert record["outcome"] == "unknown"
    assert not record_out.with_name(f"{gate.POSITIVE}.measurement.json").exists()


@pytest.mark.parametrize(
    ("config_text", "reason"),
    [(None, "a7_measurement_config_missing"), ("{}", "a7_measurement_config_invalid")],
)
def test_case_runner_with_grant_refuses_closed_without_a_valid_config(tmp_path: Path, config_text: str | None, reason: str) -> None:
    env = {"MYCELIUM_A7_FLEET_GRANT": gate.EXACT_FLEET_GRANT}
    if config_text is not None:
        config = tmp_path / "config.json"
        config.write_text(config_text, encoding="utf-8")
        env["MYCELIUM_A7_PHYSICAL_CONFIG"] = str(config)
    completed, record_out = _run_case_runner(tmp_path, env)
    assert completed.returncode == 1
    record = json.loads(record_out.read_text(encoding="utf-8"))
    assert record["reason"] == reason
    assert gate.evaluate(gate.build_plan(CANDIDATE), {gate.POSITIVE: record})["passed"] is False


def test_measure_case_refuses_without_the_exact_grant() -> None:
    with pytest.raises(MeasurementRefused) as refused:
        measure_case(plan=gate.build_plan(CANDIDATE), case=gate.POSITIVE, grant="FLEET GRANTED TO A6", config_path="/nonexistent")
    assert refused.value.code == "fleet_grant_missing"


def test_fault_target_refuses_ambiguous_missing_and_own_groups(tmp_path: Path) -> None:
    marker = "a7-fault-target-" + uuid.uuid4().hex
    script = f"import time; time.sleep(60)  # {marker}"
    processes = [subprocess.Popen([sys.executable, "-c", script], start_new_session=True) for _ in range(2)]
    try:
        time.sleep(0.3)
        with pytest.raises(MeasurementRefused) as ambiguous:
            SourceProcessGroupTarget(transport="local", process_pattern=re.escape(marker)).discover()
        assert ambiguous.value.code == "a7_fault_target_not_unique"
        with pytest.raises(MeasurementRefused) as missing:
            SourceProcessGroupTarget(transport="local", process_pattern=re.escape(marker + "-absent")).discover()
        assert missing.value.code == "a7_fault_target_not_found"
        processes[1].kill()
        processes[1].wait(timeout=10)
        target = SourceProcessGroupTarget(transport="local", process_pattern=re.escape(marker))
        group = target.discover()
        assert group.pgid == processes[0].pid
        killed = target.kill(group)
        assert killed.kill_exit_status == 0
        assert processes[0].wait(timeout=10) == -9
        assert target.group_gone(group, timeout_s=5.0) is True
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    own = subprocess.Popen([sys.executable, "-c", script])  # same process group as this test
    try:
        time.sleep(0.3)
        with pytest.raises(MeasurementRefused) as unsafe:
            SourceProcessGroupTarget(transport="local", process_pattern=re.escape(marker)).discover()
        assert unsafe.value.code == "a7_fault_target_unsafe"
    finally:
        own.kill()
        own.wait(timeout=10)
    with pytest.raises(MeasurementRefused):
        SourceProcessGroupTarget(transport="ssh", process_pattern="x", ssh_target="-oProxyCommand=evil")


def test_product_http_driver_parses_the_ordinary_gateway_stream() -> None:
    """Parsing only: a loopback stand-in for the gateway's wire shapes (no record is built)."""

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from mycelium_a7_physical_measurement import WORKSPACE_ATTRIBUTES, ProductHttpDriver

    seen: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def _json(self, document: Any, *, status: int = 200, cookie: str | None = None) -> None:
            body = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if cookie is not None:
                self.send_header("Set-Cookie", f"mycelium_session={cookie}; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            # Like the UI gateway: every route but bootstrap needs the session cookie.
            if self.path == "/api/v1/bootstrap":
                self._json({"session": {"csrf_header": "X-CSRF", "csrf_token": "t" * 32}}, cookie="s1")
            elif "mycelium_session=s1" not in (self.headers.get("Cookie") or ""):
                self._json({"error": {"code": "session_required"}}, status=401)
            elif self.path == "/api/v1/qualification/current":
                self._json({"binding": {"qualification_digest": "sha256:" + "1" * 64}})
            elif self.path == "/api/v1/product/snapshot":
                attributes = {key: 1 for key in WORKSPACE_ATTRIBUTES["inference"]}
                entities = [] if seen.get("entity_missing") else [{"kind": "recovery", "entity_id": "a7-kv-successor-runtime", "attributes": attributes}]
                self._json({"snapshot": {"entities": entities}})
            elif self.path == "/api/v1/inference/r1/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for index, token in enumerate((11, 12, 13)):
                    data = {"protocol": "p", "request_id": "r1", "sequence": index + 1, "type": "token", "token_index": index, "text": "x", "token_id": token, "publisher_generation": 1}
                    self.wfile.write(f"id: 1:{index + 1}\nevent: token\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.write(b'id: 1:9\nevent: completed\ndata: {"type": "completed"}\n\n')
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            seen["csrf"] = self.headers.get("X-CSRF")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._json({"request_id": "r1", "event_path": "/api/v1/inference/r1/events"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        driver = ProductHttpDriver(f"http://127.0.0.1:{server.server_address[1]}", prompt="hello", max_new_tokens=8)
        running = driver.start("baseline")
        assert running.done.wait(timeout=10)
        tokens, terminals = running.snapshot()
        assert [(event.index, event.token_id, event.publisher_generation) for event in tokens] == [(0, 11, 1), (1, 12, 1), (2, 13, 1)]
        assert [kind for kind, _ in terminals] == ["completed"]
        assert running.request_id == "r1" and running.error is None
        assert seen["csrf"] == "t" * 32 and seen["body"]["max_new_tokens"] == 8
        assert driver.workspaces_checked() == ["inference"]
        assert driver.workspaces_unchecked_reason is None
        assert driver.ordinary_browser_request is True
        # An empty result says why: here the product publishes no A7 entity.
        seen["entity_missing"] = True
        assert driver.workspaces_checked() == []
        assert driver.workspaces_unchecked_reason == "a7_workspace_entity_missing"
    finally:
        server.shutdown()
        server.server_close()


def _projected_a7_entity(product_evidence: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from mycelium_product_spine.contracts import A7_RECOVERY_ENTITY_ID, validate_product_snapshot
    from mycelium_product_spine.projector import ProductProjector

    snapshot = ProductProjector(pseudonym_salt=b"s" * 32).project(
        members=[], route_status=None, qualification=None, now_unix_ms=int(time.time() * 1000), a7_status=product_evidence
    )
    validated = validate_product_snapshot(snapshot)
    entity = next(item for item in validated["entities"] if item["entity_id"] == A7_RECOVERY_ENTITY_ID)
    source = next(item for item in validated["source_states"] if item["source_id"] == "a7-source")
    return entity, source


@needs_sidecar
def test_product_evidence_projects_the_a7_workspaces_from_real_entry_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mycelium_a7_physical_measurement import WORKSPACE_ATTRIBUTES

    record, observations, _plan, _scoped = _measure_with_harness(tmp_path, monkeypatch, qualification_expired=False)
    product = observations["_product_evidence"]
    assert product is not None and product["protocol"] == "mycelium.a7_product_evidence.v1"
    entity, source = _projected_a7_entity(product)
    assert source["status"] == "current"
    attributes = entity["attributes"]
    reasons = attributes["unavailable_reasons"]
    # The positive cutover leaves every field measured except the A6 recovery
    # count (no A6 recovery source in this harness).
    assert set(reasons) == {"terminal_blocked_count"}
    assert attributes["recovery_path_chosen"] == "fenced_kv_successor"
    assert attributes["recovery_prefill_operations"] == 0
    assert attributes["duplicate_token_count"] == 0
    # Coverage ends at the cutover: the covered attempt's committed watermark.
    assert attributes["committed_token_count"] == record["committed_before_failure"]
    assert attributes["fence_ledger_state"] == "empty"
    assert attributes["parity_challenge_status"] == "passed"
    assert attributes["runtime_backend"] == "mlx"
    assert attributes["process_exited_incident_count"] == 1
    assert attributes["scoped_incident_count_1h"] >= 1
    assert isinstance(attributes["incident_reason_code"], str)  # the newest scoped incident's reason
    assert attributes["transport_rtt_to_standby_ms"] > 0
    assert attributes["last_ack_rtt_ms"] > 0 and attributes["last_checkpoint_bytes_per_sec"] > 0
    assert attributes["ack_timeout_s"] == 5.0 and attributes["command_timeout_ms"] == 900_000
    assert attributes["entry_node_binding"].startswith("device-")
    assert attributes["entry_node_binding"] == attributes["standby_node_binding"] != attributes["source_node_binding"]
    for workspace, keys in WORKSPACE_ATTRIBUTES.items():
        assert all(key in attributes for key in keys), workspace
    assert record["outcome"] == "fenced_kv_successor"


def test_a7_entity_contract_refuses_defaulted_or_unexplained_values() -> None:
    from mycelium_product_spine.contracts import A7_RECOVERY_ATTRIBUTE_TYPES, _a7_recovery

    attributes: dict[str, Any] = {key: None for key in A7_RECOVERY_ATTRIBUTE_TYPES}
    reasons = {key: "not_observed" for key in A7_RECOVERY_ATTRIBUTE_TYPES}
    assert _a7_recovery({**attributes, "unavailable_reasons": reasons}) is True
    # A null without a stated reason is refused.
    assert _a7_recovery({**attributes, "unavailable_reasons": {**reasons, "last_ack_rtt_ms": None} | {}}) is False
    missing = dict(reasons)
    del missing["last_ack_rtt_ms"]
    assert _a7_recovery({**attributes, "unavailable_reasons": missing}) is False
    # A value and a reason for the same field is refused.
    assert _a7_recovery({**attributes, "last_ack_rtt_ms": 1.5, "unavailable_reasons": reasons}) is False
    observed = dict(reasons)
    del observed["last_ack_rtt_ms"]
    assert _a7_recovery({**attributes, "last_ack_rtt_ms": 1.5, "unavailable_reasons": observed}) is True
    assert _a7_recovery({**attributes, "last_ack_rtt_ms": -1.0, "unavailable_reasons": observed}) is False
    assert _a7_recovery({**attributes, "recovery_path_chosen": "maybe", "unavailable_reasons": {k: v for k, v in reasons.items() if k != "recovery_path_chosen"}}) is False
    assert _a7_recovery({**attributes, "extra": 1, "unavailable_reasons": reasons}) is False


def test_ssh_fault_target_fails_dead_links_with_keepalives() -> None:
    target = SourceProcessGroupTarget(
        transport="ssh", process_pattern="x", ssh_target="node@peer.example")
    argv = target._ssh_argv("true")
    assert argv[0] == "ssh" and argv[-2:] == ["node@peer.example", "true"]
    assert "ServerAliveInterval=10" in argv
    assert "ServerAliveCountMax=6" in argv


def test_workspaces_checked_records_a_product_refusal() -> None:
    from mycelium_a7_physical_measurement import ProductHttpDriver

    # Nothing listens on this origin: the refusal code is recorded, not swallowed.
    driver = ProductHttpDriver("http://127.0.0.1:9", prompt="hello", max_new_tokens=8)
    assert driver.workspaces_checked() == []
    assert driver.workspaces_unchecked_reason == "a7_product_http_get_unavailable"
