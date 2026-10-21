# SPDX-License-Identifier: AGPL-3.0-only
"""The A10 gate runner executes all twelve required runtime-batch negatives.

The product API drives three (cancellation isolation, queue overflow, slow
consumer); the other nine run through the opt-in fault hooks of
``mycelium_router.a10_fault_hooks`` on CPU.  A CPU hook execution is recorded as
executed but ``physical_pending``: it is never read as the physical two-Mac run.

  test_the_gate_runner_executes_all_twelve_negatives
      The runner still recording a required negative as ``not_executed``.
  test_the_cli_negatives_subcommand_executes_the_hooks_only_with_the_grant
      A grant-less run silently passing, or the opt-out being ignored.
  test_hook_negatives_are_marked_physical_pending_and_block_qualify_by_default
      A CPU hook run read as the physical two-Mac execution.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import copy
import json

import pytest

from mycelium_a10_verify_control import EXACT_FLEET_GRANT
from mycelium_router.a10_fault_hooks import FAULT_KINDS
from mycelium_router.a10_fault_negatives import CPU_SURFACE
from mycelium_router.a10_qualification import REQUIRED_RUNTIME_BATCH_NEGATIVES
from scripts import run_a10_physical_gate as gate
from tests.a10_deterministic.test_run_a10_physical_gate import SaturatedProduct


class ProductApi:
   """The three negatives the ordinary product API drives, as a passing double."""

   def cancellation_isolation(self, shape):
      return {"outcome": "failed_closed_within_scope", "residue": 0}

   def queue_overflow(self, shape):
      return {"outcome": "failed_closed_within_scope", "residue": 0}

   def slow_consumer(self, shape):
      return {"outcome": "failed_closed_within_scope", "residue": 0}


# ---------------------------------------------------------------------------
# The gate runner
# ---------------------------------------------------------------------------


def test_the_gate_runner_executes_all_twelve_negatives(tmp_path):
   runner = gate.fault_negative_runner(EXACT_FLEET_GRANT)
   results = gate.run_product_negatives(ProductApi(), out=tmp_path, fault_runner=runner)

   assert set(results) == set(REQUIRED_RUNTIME_BATCH_NEGATIVES)
   assert [case for case, item in results.items() if item["outcome"] != "failed_closed_within_scope"] == []
   assert [case for case, item in results.items() if item["residue"] != 0] == []
   assert not any(item.get("reason") == "requires_fault_hook" for item in results.values())
   recorded = json.loads((tmp_path / "negatives.json").read_text("utf-8"))
   assert recorded == json.loads(json.dumps(results))
   assert {case for case, item in recorded.items() if item.get("physical_pending")} == set(FAULT_KINDS)


def test_qualify_no_longer_blocks_on_unexecuted_negatives(tmp_path):
   product = SaturatedProduct()
   gate.run_benchmark(product, switch_mode=product.switch, observe_mode=product.observe, out=tmp_path, time_scale=0.0)
   gate.run_product_negatives(ProductApi(), out=tmp_path, fault_runner=gate.fault_negative_runner(EXACT_FLEET_GRANT))

   bundle = gate.assemble_and_qualify(
      tmp_path,
      benchmark_manifest_digest="sha256:" + "4" * 64,
      cross_host_clock_uncertainty=0.001,
      provenance="fixture",
   )
   assert [b for b in bundle["blockers"] if b.startswith("runtime_batch_negative_not_passed")] == []


def test_a_runner_without_a_fault_hook_still_records_the_negatives_unexecuted(tmp_path):
   results = gate.run_product_negatives(ProductApi(), out=tmp_path)
   assert {case for case, item in results.items() if item["outcome"] == "not_executed"} == set(FAULT_KINDS)
   assert results["timeout"] == {"outcome": "not_executed", "reason": "requires_fault_hook", "residue": None}


def test_a_violated_hook_negative_is_recorded_violated_not_dropped(tmp_path):
   def broken(kind):
      return {"outcome": "violated", "residue": 2, "fault": kind, "surface": CPU_SURFACE, "physical_pending": True}

   results = gate.run_product_negatives(ProductApi(), out=tmp_path, fault_runner=broken)
   assert results["timeout"]["outcome"] == "violated"


def test_a_raising_hook_is_recorded_violated_by_code_only(tmp_path):
   def exploding(kind):
      raise RuntimeError("/private/path/with/secret")

   results = gate.run_product_negatives(ProductApi(), out=tmp_path, fault_runner=exploding)
   assert results["timeout"] == {
      "outcome": "violated",
      "reason": "fault_hook_raised",
      "residue": None,
      "surface": CPU_SURFACE,
      "physical_pending": True,
   }
   assert "secret" not in (tmp_path / "negatives.json").read_text("utf-8")


class _StubClient(ProductApi):
   def __init__(self, base_url):
      self.base_url = base_url


@pytest.fixture
def stubbed_product(monkeypatch):
   monkeypatch.setattr(gate, "ProductClient", _StubClient)


def test_the_cli_negatives_subcommand_executes_the_hooks_only_with_the_grant(
   stubbed_product, monkeypatch, tmp_path, capsys
):
   monkeypatch.setenv("MYCELIUM_A10_FLEET_GRANT", EXACT_FLEET_GRANT)
   assert gate.main(["negatives", "--base-url", "http://127.0.0.1:1", "--out", str(tmp_path / "granted")]) == 0
   printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
   assert set(printed) == set(REQUIRED_RUNTIME_BATCH_NEGATIVES)
   assert set(printed.values()) == {"failed_closed_within_scope"}

   monkeypatch.delenv("MYCELIUM_A10_FLEET_GRANT")
   assert gate.main(["negatives", "--base-url", "http://127.0.0.1:1", "--out", str(tmp_path / "ungranted")]) == 0
   recorded = json.loads((tmp_path / "ungranted" / "negatives.json").read_text("utf-8"))
   assert {recorded[case]["reason"] for case in FAULT_KINDS} == {"fault_hook_grant_missing"}
   assert {recorded[case]["outcome"] for case in FAULT_KINDS} == {"not_executed"}

   monkeypatch.setenv("MYCELIUM_A10_FLEET_GRANT", EXACT_FLEET_GRANT)
   assert (
      gate.main(["negatives", "--no-fault-hooks", "--base-url", "http://127.0.0.1:1", "--out", str(tmp_path / "optout")])
      == 0
   )
   recorded = json.loads((tmp_path / "optout" / "negatives.json").read_text("utf-8"))
   assert {recorded[case]["reason"] for case in FAULT_KINDS} == {"requires_fault_hook"}


# ---------------------------------------------------------------------------
# CPU hook execution is not the physical two-Mac execution
# ---------------------------------------------------------------------------


def _decided_out(tmp_path):
   (tmp_path / "benchmark-summary.json").write_text(json.dumps({"browser_sessions": {"passed": True}}), "utf-8")
   results = gate.run_product_negatives(
      ProductApi(), out=tmp_path, fault_runner=gate.fault_negative_runner(EXACT_FLEET_GRANT)
   )
   return results


def test_hook_negatives_are_marked_physical_pending_and_block_qualify_by_default(tmp_path):
   _decided_out(tmp_path)
   bundle = {"decision": "qualified", "blockers": [], "qualification_binding": "sha256:" + "5" * 64}

   decision = gate.gate_decision(tmp_path, copy.deepcopy(bundle))

   assert decision["decision"] == "not_qualified"
   assert decision["physical_negatives_pending"] == sorted(FAULT_KINDS)
   assert decision["blockers"] == sorted(f"physical_negative_execution_pending:{kind}" for kind in FAULT_KINDS)


def test_the_owner_may_accept_cpu_hook_negatives_explicitly_and_the_pending_list_stays(tmp_path):
   _decided_out(tmp_path)
   bundle = {"decision": "qualified", "blockers": [], "qualification_binding": "sha256:" + "5" * 64}

   decision = gate.gate_decision(tmp_path, copy.deepcopy(bundle), accept_cpu_fault_hook_negatives=True)

   assert decision["decision"] == "qualified"
   assert decision["blockers"] == []
   assert decision["physical_negatives_pending"] == sorted(FAULT_KINDS), "accepted, never hidden"


def test_a_decision_without_hook_negatives_has_nothing_pending(tmp_path):
   (tmp_path / "benchmark-summary.json").write_text(json.dumps({"browser_sessions": {"passed": True}}), "utf-8")
   (tmp_path / "negatives.json").write_text(
      json.dumps({case: {"outcome": "failed_closed_within_scope", "residue": 0} for case in REQUIRED_RUNTIME_BATCH_NEGATIVES}),
      "utf-8",
   )
   bundle = {"decision": "qualified", "blockers": [], "qualification_binding": "sha256:" + "5" * 64}

   decision = gate.gate_decision(tmp_path, bundle)

   assert decision["decision"] == "qualified"
   assert decision["physical_negatives_pending"] == []


def test_the_qualify_cli_exposes_the_explicit_acceptance_flag():
   arguments = gate.parser().parse_args(
      [
         "qualify",
         "--out", "/tmp/x",
         "--benchmark-manifest-digest", "sha256:" + "4" * 64,
         "--cross-host-clock-uncertainty", "0.1",
         "--accept-cpu-fault-hook-negatives",
      ]
   )
   assert arguments.accept_cpu_fault_hook_negatives is True
   default = gate.parser().parse_args(
      ["qualify", "--out", "/tmp/x", "--benchmark-manifest-digest", "sha256:" + "4" * 64, "--cross-host-clock-uncertainty", "0.1"]
   )
   assert default.accept_cpu_fault_hook_negatives is False
