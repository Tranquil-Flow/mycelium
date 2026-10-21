# SPDX-License-Identifier: AGPL-3.0-only
"""The A10 physical gate driver (``scripts/run_a10_physical_gate.py``).

``verifier-matrix`` is executed end to end on a tiny offline Qwen2 snapshot written
in the Hugging Face layout (``config.json`` + ``model.safetensors``), on MLX and on
NumPy, through the driver's own CLI.  The benchmark path is driven against a
deterministic in-process product double that models a saturated server, so the
schedule, calibration, per-window minima, mode evidence, parity and the frozen v2
decision are all exercised without a network or a model.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import json
import stat
import threading
from pathlib import Path

import mlx.core as mx
import pytest

from scripts import run_a10_physical_gate as gate
from test_router_numpy_qwen_kv import _config, _tensors

REVISION = "1234567890abcdef1234567890abcdef12345678"


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
   root = tmp_path_factory.mktemp("tiny-qwen2-snapshot")
   (root / "config.json").write_text(json.dumps(_config("qwen2")), "utf-8")
   mx.save_safetensors(
      str(root / "model.safetensors"),
      {key: mx.array(value) for key, value in _tensors("qwen2").items()},
   )
   return root


@pytest.mark.parametrize("backend", ["mlx", "numpy"])
def test_verifier_matrix_cli_executes_and_decides_the_matrix(snapshot, tmp_path, backend, capsys):
   out = tmp_path / f"verifier-{backend}"
   code = gate.main(
      [
         "verifier-matrix",
         "--snapshot", str(snapshot),
         "--model-id", "local/tiny-qwen2",
         "--revision", REVISION,
         "--backend", backend,
         "--dtype", "float32",
         "--quantization", "none",
         "--widths", "1,3,6",
         "--short-prefix", "2,3",
         "--long-prefix", "20,24",
         "--operations", "102",
         "--provenance", "fixture",
         "--out", str(out),
      ]
   )

   assert code == 0
   printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
   assert printed["matrix_passed"] is True
   assert printed["decision"] == "not_qualified"
   assert printed["blockers"] == [
      "direct_runtime_call_cannot_qualify",
      "non_live_provenance_cannot_qualify",
   ]
   bundle = json.loads((out / "verifier-qualification.json").read_text("utf-8"))
   assert bundle["operation_count"] == 102
   assert bundle["parity_failures"] == 0
   assert bundle["width_limits"] == {"one": 1, "qualified_default": 3, "qualified_maximum": 6}
   assert stat.S_IMODE(out.stat().st_mode) == 0o700
   for name in ("verifier-operations.json", "verifier-negatives.json", "verifier-qualification.json"):
      assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
   operations = json.loads((out / "verifier-operations.json").read_text("utf-8"))
   assert {record["execution_path"] for record in operations} == {"direct_runtime"}
   assert all("tokens" not in record and "candidates" not in record for record in operations)


class SaturatedProduct:
   """A deterministic product double: one server, per-token service time per mode."""

   def __init__(self, *, candidate_speedup: float = 1.18, publications: bool = True) -> None:
      self.mode = "baseline"
      self.switches: list[str] = []
      self._lock = threading.Lock()
      self._server_free = 0.0
      self._speedup = candidate_speedup
      self._publications = publications

   def switch(self, mode: str) -> None:
      self.mode = mode
      self.switches.append(mode)
      with self._lock:
         self._server_free = 0.0

   def run(self, shape):
      tokens = gate.OUTPUT_BUCKETS[shape.output_bucket]
      per_token = 0.02 if self.mode == "baseline" else 0.02 / self._speedup
      with self._lock:
         start = max(self._server_free, shape.arrival_offset)
         first = start + per_token
         finished = start + tokens * per_token
         self._server_free = finished
      return gate.RequestResult(
         shape=shape,
         outcome="completed",
         submitted_at=shape.arrival_offset,
         first_token_at=first,
         finished_at=finished,
         tokens=tokens,
         output_digest="sha256:" + f"{shape.prompt_bucket}{shape.output_bucket}{shape.index}".encode().hex().ljust(64, "0")[:64],
      )

   def observe(self, mode, since):
      return "verified" if self._publications else "absent"


def test_benchmark_runs_the_registered_schedule_and_decides_materiality(tmp_path):
   product = SaturatedProduct()
   summary = gate.run_benchmark(
      product, switch_mode=product.switch, observe_mode=product.observe, out=tmp_path, time_scale=0.0
   )

   run = json.loads((tmp_path / "benchmark-run.json").read_text("utf-8"))
   assert [window["mode"] for window in run["windows"]] == [
      expected["mode"] for expected in gate.MEASURED_WINDOWS
   ]
   assert len(run["windows"]) == 32
   assert run["calibration"]["selected_offered_arrival_rate_per_second"] == 4.0
   assert product.switches[:3] == ["baseline", "baseline", "candidate"]
   assert summary["throughput"]["decision"] == "material"
   assert summary["throughput"]["valid_pair_count"] == 16
   assert summary["output_parity"]["mismatched_requests"] == 0
   assert summary["output_parity"]["compared_requests"] > 0
   window = json.loads((tmp_path / "benchmark-windows" / "window-00.json").read_text("utf-8"))
   assert window["meta"]["late_arrivals"] >= gate.MINIMUM_LATE_ARRIVALS
   assert window["meta"]["interactive"] >= gate.MINIMUM_INTERACTIVE
   assert window["meta"]["prompt_buckets"] == 2 and window["meta"]["output_buckets"] == 2
   assert window["outcomes"]["completed"] >= 60


def test_windows_without_mode_evidence_are_invalid_not_batched(tmp_path):
   product = SaturatedProduct(publications=False)
   summary = gate.run_benchmark(
      product, switch_mode=product.switch, observe_mode=product.observe, out=tmp_path, time_scale=0.0
   )
   assert summary["throughput"]["decision"] != "material"
   assert "missing_measurement" in summary["throughput"]["reasons"]


def test_an_immaterial_candidate_is_not_material(tmp_path):
   product = SaturatedProduct(candidate_speedup=1.02)
   summary = gate.run_benchmark(
      product, switch_mode=product.switch, observe_mode=product.observe, out=tmp_path, time_scale=0.0
   )
   assert summary["throughput"]["decision"] == "not_material"


def test_mode_evidence_is_scoped_to_the_window_just_run():
   publication = {
      "clock": {"monotonic_anchor": 10.0, "wall_anchor": 1_000.0},
      "intervals": [
         {"started_at": 11.0, "runtime_batch_ref": "batch-1"},
         {"started_at": 30.0, "runtime_batch_ref": ""},
      ],
   }
   status = {"peers": [{"a10_publication": publication}, {"a10_publication": None}]}
   assert gate.publications_from_status(status) == [publication]
   assert gate.batched_intervals_since([publication], 1_000.5) == 1
   assert gate.batched_intervals_since([publication], 1_002.0) == 0

   class Client:
      def live_status(self):
         return status

   observe = gate.mode_observer(Client())
   assert observe("candidate", 1_000.5) == "verified"
   assert observe("candidate", 1_002.0) == "contradicted"
   assert observe("baseline", 1_002.0) == "verified"
   assert observe("baseline", 1_000.5) == "contradicted"

   class Silent:
      def live_status(self):
         return {"peers": [{"node_id": "node-0"}]}

   assert gate.mode_observer(Silent())("candidate", 0.0) == "absent"


def test_negatives_record_unexecuted_cases_and_qualify_blocks(tmp_path):
   product = SaturatedProduct()
   gate.run_benchmark(product, switch_mode=product.switch, observe_mode=product.observe, out=tmp_path, time_scale=0.0)

   class Client:
      def cancellation_isolation(self, shape):
         return {"outcome": "failed_closed_within_scope", "residue": 0}

   negatives = gate.run_product_negatives(Client(), out=tmp_path)
   assert negatives["cancellation_isolation"]["outcome"] == "failed_closed_within_scope"
   assert negatives["timeout"] == {"outcome": "not_executed", "reason": "requires_fault_hook", "residue": None}

   bundle = gate.assemble_and_qualify(
      tmp_path,
      benchmark_manifest_digest="sha256:" + "4" * 64,
      cross_host_clock_uncertainty=0.001,
      provenance="fixture",
   )
   assert bundle["decision"] == "not_qualified"
   assert "no_observed_runtime_batch" in bundle["blockers"]
   assert "runtime_batch_negative_not_passed:timeout" in bundle["blockers"]
   assert "residual_resources_nonzero" in bundle["blockers"]
   assert "non_live_provenance_cannot_qualify" in bundle["blockers"]
   assert stat.S_IMODE((tmp_path / "runtime-batch-qualification.json").stat().st_mode) == 0o600
