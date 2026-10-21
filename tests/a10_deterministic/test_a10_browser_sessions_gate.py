# SPDX-License-Identifier: AGPL-3.0-only
"""A10 gate 1: the two-clean-browser-session requirement of the benchmark gate.

Specification §10, physical positive gate 1: at least two clean browser sessions
submit the frozen concurrent and late-arrival workload through the ordinary
gateway on one qualified multi-stage route, and the physical runtimes execute
true batches.  ``scripts/run_a10_physical_gate.py benchmark`` now runs the
automated harness (``ui/web/scripts/a10-browser-sessions.mjs``) and decides its
report; these tests pin that decision in-process (no browser, no child process,
no listen socket).  The end-to-end run with a real browser is
``tests/a10_browser``.
"""

# ruff: noqa: E402 -- the lane worktree must be importable before the gate is.

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import copy
import hashlib
import json
import stat

import pytest

from mycelium_router.a10_live_publication import pseudonym
from scripts import run_a10_physical_gate as gate


def _report(workload, *, mode="live"):
   requests = []
   ordinal = 0
   for round_ in workload["rounds"]:
      for item in round_["submissions"]:
         ordinal += 1
         record = {
            "session": item["session"],
            "round": round_["round"],
            "index": item["index"],
            "request_ref": pseudonym("request", f"request-{ordinal}"),
            "max_new_tokens": item["max_new_tokens"],
            "qos_class": item["qos_class"],
            "accepted_at_ms": 1_000 + ordinal,
            "submitted_through_ui": True,
            "frozen_submission_verified": True,
            "terminal": "completed",
            "tokens": item["max_new_tokens"],
            "terminal_observed_at_ms": 2_000 + ordinal,
         }
         if round_["round"] == "late_arrival" and item["session"] == "a":
            record["first_token_observed_at_ms"] = 1_500
         if round_["round"] == "late_arrival" and item["session"] == "b":
            record["arrived_while_leader_decoding"] = True
         requests.append(record)
   return {
      "protocol": gate.BROWSER_PROTOCOL,
      "mode": mode,
      "engine": "chromium",
      "origin": "http://127.0.0.1:8795",
      "deployment_id": "deployment-1",
      "workload_digest": workload["workload_digest"],
      "independent_sessions": 2,
      "checks": list(gate.REQUIRED_BROWSER_CHECKS),
      "failures": [],
      "rounds": {"concurrent": {}, "late_arrival": {}},
      "requests": requests,
      "request_count": 120,
      "passed": True,
      "live_evidence": mode == "live",
      "qualification_claim": False,
      "physical_claim": False,
   }


def _publication(intervals):
   return {"clock": {"monotonic_anchor": 0.0, "wall_anchor": 0.0}, "intervals": intervals}


def _interval(ref, batch="", n=0):
   return {"interval_ref": f"interval:node:{ref[-6:]}:{n}:{batch}", "request_ref": ref,
           "runtime_batch_ref": batch, "started_at": 1.0, "finished_at": 1.0}


def test_the_frozen_browser_workload_is_the_benchmark_workload_shape():
   workload = gate.browser_workload()
   assert workload["protocol"] == gate.BROWSER_WORKLOAD_PROTOCOL
   assert workload["sessions"] == ["a", "b"]
   assert [round_["round"] for round_ in workload["rounds"]] == ["concurrent", "late_arrival"]
   submissions = [item for round_ in workload["rounds"] for item in round_["submissions"]]
   assert [item["session"] for item in submissions] == ["a", "b", "a", "b"]
   for item in submissions:
      shape = gate.RequestShape(item["index"], "prompt_short", "output_long", "interactive", 0.0)
      # Same prompt text, token bound and QoS rule as the scored windows.
      assert item["prompt"] == gate.prompt_text(shape)
      assert item["max_new_tokens"] in gate.OUTPUT_BUCKETS.values()
      assert item["qos_class"] == "interactive"
      assert item["workload_profile_id"] == "interactive_chat_v1"
   # Equal prompt lengths, so concurrent members can share a decode position.
   assert len({len(item["prompt"]) for item in submissions}) == 1
   assert submissions[2]["max_new_tokens"] == gate.OUTPUT_BUCKETS["output_long"]
   body = {key: value for key, value in workload.items() if key != "workload_digest"}
   canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
   assert workload["workload_digest"] == "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
   assert gate.browser_workload() == workload


def test_a_passing_live_report_is_accepted_and_reduced_to_public_fields():
   workload = gate.browser_workload()
   decided = gate.validate_browser_report(_report(workload), workload=workload, mode="live")
   assert decided["engine"] == "chromium"
   assert len(decided["request_refs"]) == 4
   assert decided["deployment_id"] == "deployment-1"


@pytest.mark.parametrize(
   ("mutate", "code"),
   [
      (lambda r: r.update(passed=False), "browser_report_not_passed"),
      (lambda r: r.update(failures=["a:pageerror"]), "browser_report_not_passed"),
      (lambda r: r.update(checks=r["checks"][:-1]), "browser_report_checks_incomplete"),
      (lambda r: r.update(mode="local_conformance", live_evidence=False), "browser_report_mode_mismatch"),
      (lambda r: r.update(live_evidence=False), "browser_report_not_live_evidence"),
      (lambda r: r.update(qualification_claim=True), "browser_report_claim_invalid"),
      (lambda r: r.update(workload_digest="sha256:" + "0" * 64), "browser_report_workload_mismatch"),
      (lambda r: r.update(independent_sessions=1), "browser_report_sessions_invalid"),
      (lambda r: r.update(protocol="mycelium.a14_live_browser_gate.v1"), "browser_report_protocol_invalid"),
      (lambda r: r.update(extra=1), "browser_report_shape_invalid"),
      (lambda r: r["requests"].pop(), "browser_report_requests_invalid"),
      (lambda r: r["requests"][1].update(session="a"), "browser_report_requests_invalid"),
      (lambda r: r["requests"][0].update(terminal="cancelled"), "browser_report_requests_invalid"),
      (lambda r: r["requests"][0].update(tokens=0), "browser_report_requests_invalid"),
      (lambda r: r["requests"][0].update(submitted_through_ui=False), "browser_report_requests_invalid"),
      (lambda r: r["requests"][0].update(frozen_submission_verified=False), "browser_report_requests_invalid"),
      (lambda r: r["requests"][3].update(arrived_while_leader_decoding=False), "browser_report_requests_invalid"),
      (lambda r: r["requests"][1].update(request_ref=r["requests"][0]["request_ref"]), "browser_report_requests_invalid"),
      (lambda r: r["requests"][0].update(request_ref="request-1"), "browser_report_requests_invalid"),
      (lambda r: r["requests"][0].update(prompt="A10 benchmark request 0:"), "browser_report_requests_invalid"),
   ],
)
def test_every_incomplete_or_mislabelled_report_is_refused(mutate, code):
   workload = gate.browser_workload()
   report = _report(workload)
   mutate(report)
   with pytest.raises(gate.GateError) as refused:
      gate.validate_browser_report(report, workload=workload, mode="live")
   assert str(refused.value) == code


def test_local_conformance_reports_never_pass_as_live_and_vice_versa():
   workload = gate.browser_workload()
   local = _report(workload, mode="local_conformance")
   assert gate.validate_browser_report(local, workload=workload, mode="local_conformance")["mode"] == (
      "local_conformance"
   )
   with pytest.raises(gate.GateError, match="browser_report_mode_mismatch"):
      gate.validate_browser_report(local, workload=workload, mode="live")
   with pytest.raises(gate.GateError, match="browser_report_mode_mismatch"):
      gate.validate_browser_report(_report(workload), workload=workload, mode="local_conformance")


def test_runtime_evidence_requires_every_browser_request_on_the_route_and_a_true_batch():
   refs = [pseudonym("request", f"request-{n}") for n in range(1, 5)]
   other = pseudonym("request", "benchmark-request")
   publications = [
      _publication([_interval(refs[0], "batch-7", 1), _interval(refs[1], "batch-7", 2),
                    _interval(refs[2], "", 3), _interval(refs[3], "batch-9", 4), _interval(other, "batch-9", 5)]),
      _publication([_interval(refs[0], "", 6)]),
   ]
   evidence = gate.browser_runtime_evidence(publications, refs)
   assert evidence == {
      "browser_requests": 4,
      "executed_on_route": 4,
      "in_true_runtime_batch": 3,
      "browser_pair_in_one_batch": True,
      "true_batches_with_browser_members": 2,
   }
   assert gate.browser_runtime_blockers(evidence) == []
   missing = gate.browser_runtime_evidence(publications[:1][:0] + [_publication([_interval(refs[0], "b", 1)])], refs)
   assert "browser_request_not_executed_on_route" in gate.browser_runtime_blockers(missing)
   solo = gate.browser_runtime_evidence([_publication([_interval(ref, f"solo-{n}", n) for n, ref in enumerate(refs)])], refs)
   assert solo["in_true_runtime_batch"] == 0
   assert "browser_requests_not_in_true_runtime_batch" in gate.browser_runtime_blockers(solo)
   assert gate.browser_runtime_blockers(gate.browser_runtime_evidence([], refs)) == [
      "browser_request_not_executed_on_route",
      "browser_requests_not_in_true_runtime_batch",
   ]


class _Product:
   def __init__(self):
      self.mode = "baseline"
      self.switches = []

   def switch(self, mode):
      self.mode = mode
      self.switches.append(mode)

   def run(self, shape):
      tokens = gate.OUTPUT_BUCKETS[shape.output_bucket]
      return gate.RequestResult(shape, "completed", shape.arrival_offset, shape.arrival_offset + 0.01,
                                shape.arrival_offset + 0.01 * tokens, tokens, "sha256:" + "1" * 64)

   def observe(self, mode, since):
      return "verified"


def test_the_benchmark_runs_the_browser_phase_first_in_candidate_mode_and_seals_it(tmp_path):
   product = _Product()
   seen = []

   def browser_phase():
      seen.append(product.mode)
      return {"protocol": gate.BROWSER_EVIDENCE_PROTOCOL, "passed": True, "blockers": []}

   summary = gate.run_benchmark(product, switch_mode=product.switch, observe_mode=product.observe,
                                out=tmp_path, time_scale=0.0, browser_sessions=browser_phase)
   assert seen == ["candidate"]
   assert product.switches[:2] == ["candidate", "baseline"]
   assert summary["browser_sessions"]["passed"] is True
   sealed = tmp_path / "browser-sessions.json"
   assert json.loads(sealed.read_text("utf-8"))["passed"] is True
   assert stat.S_IMODE(sealed.stat().st_mode) == 0o600


def test_a_failed_browser_phase_stops_the_benchmark_before_calibration(tmp_path):
   product = _Product()
   calls = []
   product.run = lambda shape: calls.append(shape)  # noqa: E731 - must never be called

   def browser_phase():
      return {"protocol": gate.BROWSER_EVIDENCE_PROTOCOL, "passed": False,
              "blockers": ["browser_report_not_passed"]}

   with pytest.raises(gate.GateError, match="browser_sessions_failed:browser_report_not_passed"):
      gate.run_benchmark(product, switch_mode=product.switch, observe_mode=product.observe,
                         out=tmp_path, time_scale=0.0, browser_sessions=browser_phase)
   assert calls == []
   assert json.loads((tmp_path / "browser-sessions.json").read_text("utf-8"))["passed"] is False
   assert not (tmp_path / "benchmark-run.json").exists()


def test_without_a_browser_phase_the_summary_says_gate_one_is_unproven(tmp_path):
   product = _Product()
   summary = gate.run_benchmark(product, switch_mode=product.switch, observe_mode=product.observe,
                                out=tmp_path, time_scale=0.0)
   assert summary["browser_sessions"] == {"executed": False, "passed": False, "reason": "not_configured"}


def test_qualify_blocks_without_passed_browser_sessions(tmp_path):
   product = _Product()
   gate.run_benchmark(product, switch_mode=product.switch, observe_mode=product.observe, out=tmp_path,
                      time_scale=0.0)
   bundle = gate.assemble_and_qualify(tmp_path, benchmark_manifest_digest="sha256:" + "4" * 64,
                                      cross_host_clock_uncertainty=0.001, provenance="live")
   decision = gate.gate_decision(tmp_path, bundle)
   assert decision["decision"] == "not_qualified"
   assert "browser_sessions_not_proven" in decision["blockers"]
   assert set(bundle["blockers"]) <= set(decision["blockers"])
   assert decision["runtime_batch_qualification_binding"] == bundle["qualification_binding"]
   assert stat.S_IMODE((tmp_path / "gate-decision.json").stat().st_mode) == 0o600


def test_qualify_carries_a_passed_browser_phase_without_dropping_runtime_blockers(tmp_path):
   product = _Product()
   gate.run_benchmark(
      product, switch_mode=product.switch, observe_mode=product.observe, out=tmp_path, time_scale=0.0,
      browser_sessions=lambda: {"protocol": gate.BROWSER_EVIDENCE_PROTOCOL, "passed": True, "blockers": []},
   )
   bundle = gate.assemble_and_qualify(tmp_path, benchmark_manifest_digest="sha256:" + "4" * 64,
                                      cross_host_clock_uncertainty=0.001, provenance="live")
   decision = gate.gate_decision(tmp_path, bundle)
   assert "browser_sessions_not_proven" not in decision["blockers"]
   assert decision["decision"] == bundle["decision"]
   assert decision["browser_sessions_digest"].startswith("sha256:")
   forged = copy.deepcopy(bundle)
   forged["decision"] = "qualified"
   forged["blockers"] = []
   assert gate.gate_decision(tmp_path, forged)["decision"] == "qualified"


def test_the_benchmark_cli_refuses_before_any_origin_contact_without_a_runnable_harness(tmp_path, capsys, monkeypatch):
   contacted = []
   monkeypatch.setattr(gate, "ProductClient", lambda base_url: contacted.append(base_url))
   code = gate.main(
      [
         "benchmark", "--base-url", "http://127.0.0.1:9", "--baseline-command", "true",
         "--candidate-command", "true", "--out", str(tmp_path / "out"), "--node", str(tmp_path / "no-node"),
      ]
   )
   assert code == 2
   assert json.loads(capsys.readouterr().err) == {"error": "browser_harness_unavailable"}
   assert contacted == []
   assert not (tmp_path / "out" / "benchmark-run.json").exists()
   with pytest.raises(SystemExit):
      gate.parser().parse_args(
         ["benchmark", "--base-url", "x", "--baseline-command", "a", "--candidate-command", "b",
          "--out", "o", "--browser-engine", "opera"]
      )


def test_the_phase_refuses_an_unknown_engine_or_mode_and_a_reused_evidence_directory(tmp_path):
   with pytest.raises(gate.GateError, match="browser_engine_invalid"):
      gate.BrowserSessionsPhase(base_url="http://127.0.0.1:9", out=tmp_path, live_status=dict, engine="opera")
   with pytest.raises(gate.GateError, match="browser_mode_invalid"):
      gate.BrowserSessionsPhase(base_url="http://127.0.0.1:9", out=tmp_path, live_status=dict, mode="fixture")
   (tmp_path / "browser-sessions").mkdir()
   phase = gate.BrowserSessionsPhase(base_url="http://127.0.0.1:9", out=tmp_path, live_status=dict)
   with pytest.raises(gate.GateError, match="browser_evidence_directory_exists"):
      phase.preflight()


def test_two_browser_requests_sharing_a_runtime_batch_ref_are_in_a_true_batch():
   """Live finding f7b2: one pair of browser requests in one batch is enough."""

   refs = [pseudonym("request", f"request-{n}") for n in (1, 2)]
   shared = [
      _publication([_interval(refs[0], "batch-prefill", 1), _interval(refs[1], "batch-prefill", 2)]),
      _publication([_interval(refs[0], "batch-prefill-2", 3), _interval(refs[1], "batch-prefill-2", 4)]),
   ]
   evidence = gate.browser_runtime_evidence(shared, refs)
   assert evidence["in_true_runtime_batch"] == 2
   assert evidence["browser_pair_in_one_batch"] is True
   assert gate.browser_runtime_blockers(evidence) == []
   # One request repeating a batch ref across stages is not a batch of two.
   one = gate.browser_runtime_evidence(
      [_publication([_interval(refs[0], "batch-x", 1)]), _publication([_interval(refs[0], "batch-x", 2)])],
      refs,
   )
   assert one["in_true_runtime_batch"] == 0
   # Two requests whose intervals carry different refs (the f7b2 outcome) are not.
   split = gate.browser_runtime_evidence(
      [_publication([_interval(refs[0], "batch-a", 1), _interval(refs[1], "batch-b", 2)])], refs
   )
   assert split["in_true_runtime_batch"] == 0
   assert "browser_requests_not_in_true_runtime_batch" in gate.browser_runtime_blockers(split)
