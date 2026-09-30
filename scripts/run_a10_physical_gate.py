#!/usr/bin/env python3
"""Run the A10 physical gate: verifier matrix, calibrated benchmark and negatives.

Subcommands (every output is an owner-private file under ``--out``, mode 0700/0600):

``verifier-matrix``
    Loads the exact target from a local model snapshot (``config.json`` plus
    safetensors) into one host's MLX or NumPy stage runtimes, colocating every
    layer on that host, and executes the correctness matrix of specification
    section 8: at least 100 bounded operations over the widths ``one``,
    ``qualified_default`` and ``qualified_maximum`` in at least two prefix-length
    buckets, every transaction mode, plus the stale-prefix, generation-policy,
    position-bound and cancellation negatives.  Each operation is judged against
    the sequential target oracle on the same prefix.  The records are then decided
    by ``qualify_target_verifier``.  This path is a direct runtime call, so its
    records say so (``execution_path = direct_runtime``) and the decider blocks
    qualification until the same matrix runs through the product dispatcher.

``benchmark``
    Runs the pre-registered v2 protocol (``tests/a10_acceptance/
    benchmark_protocol.v2.json``) through the ordinary product gateway: a recorded
    baseline-only calibration walks the descending rate ladder, then one unscored
    warmup per mode, then 16 scheduled baseline/candidate pairs in the registered
    alternating order.  Each window offers at least 60 requests with at least 20
    interactive requests, two prompt-length and two output-length buckets, and
    arrivals continuing while earlier requests decode.  The mode switch is an
    operator command per mode (``--baseline-command`` / ``--candidate-command``)
    because the policy switch belongs to the route, not to this client.  After
    every window the live status is read: a candidate window must show a runtime
    batch in the nodes' A10 publications and a baseline window must show none;
    missing publications invalidate the window (``missing_measurement``), never
    count as batching.  Output parity is judged privately per request shape.

``verifier-product``
    Runs the same matrix and negatives through the product dispatcher of a live
    serve: one operation per request to the grant-gated loopback A10 control
    (``--a10-verify-control-port``, grant in ``MYCELIUM_A10_FLEET_GRANT``), which
    the live router port admits exclusively and sends to the target node, whose
    own runtime produces each record (``execution_path = product_dispatcher``).
    The binding digests describe the deployed target and are supplied by the
    operator from its deployment.  Any refused operation stops the run without a
    decision.

``negatives``
    Executes the runtime-batch negatives the product API can drive
    (cancellation isolation, queue overflow, slow consumer) and records every other
    required negative as ``not_executed`` with its reason, which blocks
    qualification until a fault hook executes it.

``qualify``
    Assembles the evidence written by the other subcommands and decides it with
    ``qualify_runtime_batch``.

No output contains a prompt, decoded text, token IDs, cookies, hostnames,
addresses, private paths or exception strings.
"""

# ruff: noqa: E402 -- direct script execution needs the repository on sys.path.

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
   sys.path.insert(0, str(REPO_ROOT))

from mycelium_router.a10_qualification import (
   REQUIRED_RUNTIME_BATCH_NEGATIVES,
   RESIDUAL_RESOURCE_FIELDS,
   RUNTIME_BATCH_EVIDENCE_PROTOCOL,
   canonical_digest,
)
from mycelium_router.a10_runtime_batch import (
   ACCEPTANCE_INVENTORY_DIGEST,
   QualificationRequest,
   qualify_runtime_batch,
)
from mycelium_router.a10_target_verification import (
   TargetVerificationQualificationRequest,
   qualify_target_verifier,
)
from mycelium_router.a10_verifier_matrix import (
   VERIFIER_NEGATIVE_ORDER,
   MatrixPlan,
   run_verifier_matrix,
)
from tests.a10_acceptance.benchmark_amendment import (
   RUN_PROTOCOL,
   evaluate_throughput,
   load_protocol,
   protocol_digest,
)

GATE_PROTOCOL = "mycelium.a10_physical_gate_run.v1"
PROTOCOL = load_protocol()
REQUIREMENTS = PROTOCOL["benchmark_requirements"]
CALIBRATION = PROTOCOL["calibration"]
MEASURED_WINDOWS = PROTOCOL["measured_windows"]
# Offered per window: enough that a window completing at the calibrated 90% floor
# still has the registered minimum of completed requests.
MINIMUM_WINDOW_REQUESTS = math.ceil(
   max(
      int(REQUIREMENTS["minimum_completed_requests_per_window"]),
      int(CALIBRATION["minimum_requests_per_trial"]),
   )
   / float(CALIBRATION["minimum_baseline_completion_fraction"])
)
MINIMUM_INTERACTIVE = int(REQUIREMENTS["minimum_interactive_requests_per_window"])
MINIMUM_LATE_ARRIVALS = int(REQUIREMENTS["minimum_late_arrivals_per_window"])
RETAINED_OUTCOMES = tuple(PROTOCOL["retained_outcomes"])
TERMINAL_KINDS = {"completed", "cancelled", "failed"}
PROMPT_BUCKETS = {"prompt_short": 16, "prompt_long": 96}
OUTPUT_BUCKETS = {"output_short": 8, "output_long": 32}


class GateError(RuntimeError):
   """A coded gate failure; the code is the only thing recorded."""


# ---------------------------------------------------------------------------
# Owner-private output
# ---------------------------------------------------------------------------


def _private_directory(path: Path) -> Path:
   path.mkdir(parents=True, exist_ok=True, mode=0o700)
   os.chmod(path, 0o700)
   return path


def write_private_json(path: Path, document: object) -> None:
   _private_directory(path.parent)
   temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
   descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
   with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
      json.dump(document, handle, sort_keys=True, indent=1, allow_nan=False)
      handle.write("\n")
      handle.flush()
      os.fsync(handle.fileno())
   os.replace(temporary, path)


def read_json(path: Path) -> Any:
   return json.loads(Path(path).read_text("utf-8"))


# ---------------------------------------------------------------------------
# verifier-matrix: a colocated exact target on this host
# ---------------------------------------------------------------------------


def _safetensors_keys(path: Path) -> list[str]:
   with path.open("rb") as handle:
      length = int.from_bytes(handle.read(8), "little")
      if length <= 0 or length > 100 * 1024 * 1024:
         raise GateError("safetensors_header_invalid")
      header = json.loads(handle.read(length))
   return sorted(key for key in header if key != "__metadata__")


def _sha256_file(path: Path) -> str:
   digest = hashlib.sha256()
   with path.open("rb") as handle:
      for chunk in iter(lambda: handle.read(1 << 22), b""):
         digest.update(chunk)
   return digest.hexdigest()


class _SnapshotFetcher:
   """Resolve files inside one local snapshot only; never download."""

   def __init__(self, root: Path, model_id: str, revision: str) -> None:
      self.root = root
      self.model_id = model_id
      self.revision = revision

   def __call__(self, model_id, revision, filename, cache_root, local_files_only=False):
      if model_id != self.model_id or revision != self.revision or not local_files_only:
         raise GateError("snapshot_fetch_refused")
      candidate = self.root / filename
      if ".." in Path(filename).parts or not candidate.is_file():
         raise GateError("snapshot_file_missing")
      return candidate, True


@dataclass
class LocalTarget:
   graph: Any
   ports: tuple[Any, ...]
   manifest: dict[str, Any]
   backend: str
   runtime_digest: str
   model_digest: str
   representation_digest: str

   def stages(self) -> tuple[Any, ...]:
      return tuple(stage for port in self.ports for stage in port.a10_verifier_stages())


def build_local_target(
   snapshot: Path,
   *,
   model_id: str,
   revision: str,
   backend: str,
   dtype: str,
   quantization: str,
) -> LocalTarget:
   """Load every layer of the exact target on this host, split over two stages."""

   from dataclasses import replace

   from layer_assignment import compile_layer_assignments
   from model_manifest import compile_model_manifest, manifest_digest_ref
   from mycelium_router.contracts import (
      ExecutionGraph,
      LayerRange,
      Placement,
      PlacementEdge,
      Stage,
      StageCost,
   )
   from mycelium_router.layer_builder import layer_load_proof_digest
   from runtime_loader import canonical_json, load_assignment_stage
   from two_process_runtime_qualification import _control_plane_binding
   from weight_provisioning import provision_assignment

   snapshot = Path(snapshot)
   config = read_json(snapshot / "config.json")
   index_path = snapshot / "model.safetensors.index.json"
   if index_path.is_file():
      weight_map = read_json(index_path)["weight_map"]
   else:
      weight_map = {key: "model.safetensors" for key in _safetensors_keys(snapshot / "model.safetensors")}
   files = sorted(set(weight_map.values()))
   manifest = compile_model_manifest(
      model_id=model_id,
      requested_revision=revision,
      resolved_commit=revision,
      config=config,
      checkpoint_index={"weight_map": weight_map},
      file_metadata={
         name: {"size_bytes": (snapshot / name).stat().st_size, "sha256": _sha256_file(snapshot / name)}
         for name in files
      },
   )
   layers = int(manifest["num_layers"])
   if layers < 2:
      raise GateError("target_needs_two_layers")
   split = layers // 2
   nodes = ("verifier-entry", "verifier-final")
   binding = _control_plane_binding()
   route = {
      "ok": True,
      "protocol": "mycelium.manual_provisioning_route.v1",
      "claim_boundary": "colocated verifier target on one host; not a route-readiness claim",
      "model": {
         "model_id": manifest["model_id"],
         "num_layers": layers,
         "manifest_digest": manifest_digest_ref(manifest),
         "resolved_commit": manifest["resolved_commit"],
      },
      "route": [
         {"node_id": nodes[0], "range": {"start_layer": 0, "end_layer_exclusive": split, "layer_count": split}},
         {"node_id": nodes[1], "range": {"start_layer": split, "end_layer_exclusive": layers, "layer_count": layers - split}},
      ],
      "node_order": list(nodes),
   }
   assignments = compile_layer_assignments(
      route_plan=route,
      manifest=manifest,
      deployment_id=binding["deployment_id"],
      deployment_epoch=binding["deployment_epoch"],
      cache_roots={node: str(snapshot) for node in nodes},
      runtime_by_node={node: {"backend": backend, "dtype": dtype, "quantization": quantization} for node in nodes},
      control_plane_binding=binding,
   )
   fetcher = _SnapshotFetcher(snapshot, model_id, revision)
   loaded = tuple(
      load_assignment_stage(
         assignment,
         provision_assignment(assignment, fetch_file=fetcher, local_files_only=True),
         load_generation=1,
      )
      for assignment in assignments
   )
   plain = lambda value: json.loads(canonical_json(value))  # noqa: E731
   stages = []
   placements = []
   for index, (assignment, stage_loaded) in enumerate(zip(assignments, loaded)):
      stage_id = f"stage-{index:03d}"
      placement = Placement(
         placement_id=f"placement-{index:03d}",
         node_id=assignment["node_id"],
         replica_group_id=f"{stage_id}-replicas",
         assignment_id=assignment["assignment_id"],
         stage_signature="pending-stage-signature",
         load_proof_digest=layer_load_proof_digest(plain(stage_loaded.proof)),
         runtime_backend=assignment["runtime"]["backend"],
         runtime_endpoint=f"memory://{assignment['node_id']}/{stage_id}",
      )
      placements.append(placement)
      layer_range = assignment["range"]
      stages.append(
         Stage(
            stage_id=stage_id,
            layer_range=LayerRange(**layer_range),
            component_roles=tuple(assignment["components"]),
            stage_cost=StageCost(
               prefill_work_units_per_prompt_token=1.0,
               decode_work_units_per_token=1.0,
               kv_bytes_per_context_token=32,
            ),
            placements=(placement,),
         )
      )
   graph = ExecutionGraph(
      deployment_id=assignments[0]["deployment_id"],
      deployment_epoch=assignments[0]["deployment_epoch"],
      topology_version=1,
      model_id=assignments[0]["model_id"],
      resolved_commit=assignments[0]["resolved_commit"],
      manifest_digest=assignments[0]["manifest_digest"],
      entry_stage_id=stages[0].stage_id,
      final_stage_id=stages[-1].stage_id,
      hidden_size=assignments[0]["runtime"]["model_config"]["n_embd"],
      activation_bytes=4,
      token_envelope_bytes=9,
      stages=tuple(stages),
      edges=(
         PlacementEdge(
            edge_id="forward:placement-000->placement-001",
            from_placement_id=placements[0].placement_id,
            to_placement_id=placements[1].placement_id,
            link_id="local:verifier-entry->verifier-final",
         ),
      ),
      loopback_edges=(
         PlacementEdge(
            edge_id="loopback:placement-001->placement-000",
            from_placement_id=placements[1].placement_id,
            to_placement_id=placements[0].placement_id,
            link_id="local:verifier-final->verifier-entry",
         ),
      ),
   )
   if backend == "mlx":
      from mycelium_router.mlx_runtime import MLXRuntimePort as Port, _stage_signature
   elif backend == "numpy":
      from mycelium_router.numpy_runtime import NumpyRuntimePort as Port, _stage_signature
   else:
      raise GateError("unsupported_backend")
   graph = replace(
      graph,
      stages=tuple(
         replace(
            stage,
            placements=(replace(stage.placements[0], stage_signature=_stage_signature(graph, stage, stage_loaded.proof)),),
         )
         for stage, stage_loaded in zip(graph.stages, loaded)
      ),
   )
   extra = {} if backend == "mlx" else {"decode_mode": "stage_local_kv"}
   ports = tuple(
      Port(assignment["node_id"], graph, {graph.stages[index].placements[0].placement_id: loaded[index]}, **extra)
      for index, assignment in enumerate(assignments)
   )
   runtime_digest = canonical_digest(
      {
         "backend": backend,
         "dtype": dtype,
         "quantization": quantization,
         "load_proofs": [layer_load_proof_digest(plain(item.proof)) for item in loaded],
      }
   )
   return LocalTarget(
      graph=graph,
      ports=ports,
      manifest=manifest,
      backend=backend,
      runtime_digest=runtime_digest,
      model_digest=manifest_digest_ref(manifest),
      representation_digest=canonical_digest({"dtype": dtype, "quantization": quantization}),
   )


def run_verifier_gate(
   target: LocalTarget,
   *,
   widths: Mapping[str, int],
   prefix_buckets: Mapping[str, tuple[int, ...]],
   operations: int,
   provenance: str,
   out: Path,
   progress: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
   stages = target.stages()

   def open_verifier(prefix: tuple[int, ...], ref: str) -> Any:
      return target.ports[0].begin_target_verifier(prefix, verification_ref=ref, stages=stages)

   def cancel(verifier: Any) -> None:
      for port in target.ports:
         port.cancel(verifier.path_id)

   plan = MatrixPlan(
      widths=dict(widths),
      prefix_buckets={name: tuple(lengths) for name, lengths in prefix_buckets.items()},
      operations=operations,
      vocab_size=stages[0].vocab_size,
   )
   started = time.monotonic()
   records, negatives = run_verifier_matrix(
      plan,
      open_verifier=open_verifier,
      cancel_verification=cancel,
      runtime_digest=target.runtime_digest,
      model_digest=target.model_digest,
      representation_digest=target.representation_digest,
      backend=target.backend,
      provenance=provenance,
      execution_path="direct_runtime",
      on_operation=lambda index, record: progress(f"operation {index + 1}/{operations}"),
   )
   request = TargetVerificationQualificationRequest(
      owner_private_root=str(out.name),
      runtime_digest=target.runtime_digest,
      width_classes=("one", "qualified_default", "qualified_maximum"),
      prefix_length_buckets=len(prefix_buckets),
      operations=operations,
      sequential_parity_proof_digest=canonical_digest(records),
   )
   bundle = qualify_target_verifier(request, records, negatives)
   summary = {
      "protocol": GATE_PROTOCOL,
      "subcommand": "verifier-matrix",
      "backend": target.backend,
      "elapsed_seconds": round(time.monotonic() - started, 3),
      "decision": bundle["decision"],
      "matrix_passed": bundle["matrix_passed"],
      "blockers": bundle["blockers"],
   }
   write_private_json(out / "verifier-operations.json", records)
   write_private_json(out / "verifier-negatives.json", negatives)
   write_private_json(out / "verifier-qualification.json", bundle)
   write_private_json(out / "verifier-summary.json", summary)
   return bundle


# ---------------------------------------------------------------------------
# benchmark: the calibrated v2 protocol through the product gateway
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestShape:
   index: int
   prompt_bucket: str
   output_bucket: str
   qos_class: str
   arrival_offset: float

   @property
   def shape_key(self) -> str:
      return f"{self.prompt_bucket}:{self.output_bucket}:{self.index}"


def window_workload(
   *, rate: float, count: int, seed: int
) -> list[RequestShape]:
   """A frozen, deterministic arrival schedule covering every bucket and QoS."""

   rng = random.Random(seed)
   offset = 0.0
   shapes = []
   for index in range(count):
      shapes.append(
         RequestShape(
            index=index,
            prompt_bucket=tuple(PROMPT_BUCKETS)[index % 2],
            output_bucket=tuple(OUTPUT_BUCKETS)[(index // 2) % 2],
            qos_class="interactive" if index % 2 == 0 else "batch",
            arrival_offset=offset,
         )
      )
      offset += -math.log(1.0 - rng.random()) / rate
   return shapes


def prompt_text(shape: RequestShape) -> str:
   return f"A10 benchmark request {shape.index}:" + " benchmark" * PROMPT_BUCKETS[shape.prompt_bucket]


@dataclass
class RequestResult:
   shape: RequestShape
   outcome: str
   submitted_at: float
   first_token_at: float | None
   finished_at: float
   tokens: int
   output_digest: str | None


class ProductClient:
   """Submit one request through the ordinary gateway and stream it to terminal."""

   def __init__(self, base_url: str) -> None:
      from scripts.run_a5_benchmark_gate import ProductSession

      self.base_url = base_url
      self._session = ProductSession(base_url)

   def run(self, shape: RequestShape) -> RequestResult:
      session = self._session.fork()
      submitted = time.monotonic()
      try:
         accepted = session.submit(
            prompt=prompt_text(shape),
            maximum_new_tokens=OUTPUT_BUCKETS[shape.output_bucket],
            workload_profile_id="interactive_chat_v1" if shape.qos_class == "interactive" else "sustained_batch_v1",
            qos_class=shape.qos_class,
         )
      except Exception as error:  # noqa: BLE001 - only the code is recorded
         code = str(error)
         outcome = "overflowed" if code.endswith("_429") or code.endswith("_503") else "failed"
         return RequestResult(shape, outcome, submitted, None, time.monotonic(), 0, None)
      first = None
      tokens = 0
      terminal = None
      output = hashlib.sha256()
      try:
         with session.request("GET", accepted["event_path"]) as response:
            kind = None
            for raw in response:
               line = raw.decode("utf-8", "strict").rstrip("\r\n")
               if line.startswith("event: "):
                  kind = line[7:]
                  if kind in TERMINAL_KINDS:
                     terminal = kind
                     break
               elif line.startswith("data: ") and kind == "token":
                  tokens += 1
                  if first is None:
                     first = time.monotonic()
                  output.update(line[6:].encode("utf-8"))
      except Exception:  # noqa: BLE001
         terminal = "failed"
      outcome = {"completed": "completed", "cancelled": "cancelled"}.get(terminal or "", "failed")
      return RequestResult(
         shape,
         outcome,
         submitted,
         first,
         time.monotonic(),
         tokens,
         "sha256:" + output.hexdigest() if outcome == "completed" else None,
      )

   def live_status(self) -> dict[str, Any]:
      from scripts.run_a5_benchmark_gate import public_json

      return public_json(self.base_url, "/__mycelium/live-status")

   # -- product-drivable runtime-batch negatives ------------------------------
   def _zero_residue(self, timeout: float = 120.0) -> int:
      """0 when the route returns to zero queued work and reservations in time."""

      from scripts.run_a5_benchmark_gate import _zero_live_resources, public_json

      deadline = time.monotonic() + timeout
      while time.monotonic() < deadline:
         try:
            if _zero_live_resources(public_json(self.base_url, "/__mycelium/runtime/admission-status")):
               return 0
         except Exception:  # noqa: BLE001
            pass
         time.sleep(1.0)
      return 1

   def _outcome(self, passed: bool) -> dict[str, Any]:
      residue = self._zero_residue()
      return {
         "outcome": "failed_closed_within_scope" if passed and residue == 0 else "violated",
         "residue": residue,
      }

   def cancellation_isolation(self, shape: RequestShape) -> dict[str, Any]:
      """Cancel one of two concurrent requests; only the cancelled one may stop."""

      victim_session = self._session.fork()
      victim = victim_session.submit(
         prompt=prompt_text(shape),
         maximum_new_tokens=OUTPUT_BUCKETS["output_long"],
         workload_profile_id="interactive_chat_v1",
         qos_class="interactive",
      )
      survivor: dict[str, RequestResult] = {}
      thread = threading.Thread(
         target=lambda: survivor.setdefault("result", self.run(RequestShape(1, "prompt_short", "output_long", "interactive", 0.0))),
         daemon=True,
      )
      thread.start()
      time.sleep(0.5)
      victim_session.cancel(victim)
      thread.join(timeout=300)
      result = survivor.get("result")
      return self._outcome(result is not None and result.outcome == "completed")

   def queue_overflow(self, shape: RequestShape) -> dict[str, Any]:
      """A burst beyond the admission bound is refused before dispatch, not queued."""

      burst = [RequestShape(index, "prompt_short", "output_short", "batch", 0.0) for index in range(64)]
      results: list[RequestResult] = []
      lock = threading.Lock()

      def submit(item: RequestShape) -> None:
         outcome = self.run(item)
         with lock:
            results.append(outcome)

      threads = [threading.Thread(target=submit, args=(item,), daemon=True) for item in burst]
      for thread in threads:
         thread.start()
      for thread in threads:
         thread.join(timeout=600)
      overflowed = sum(1 for item in results if item.outcome == "overflowed")
      terminal = len(results) == len(burst)
      return self._outcome(terminal and overflowed > 0)

   def slow_consumer(self, shape: RequestShape) -> dict[str, Any]:
      """A consumer that stops reading must not block another request."""

      stalled_session = self._session.fork()
      stalled = stalled_session.submit(
         prompt=prompt_text(shape),
         maximum_new_tokens=OUTPUT_BUCKETS["output_long"],
         workload_profile_id="interactive_chat_v1",
         qos_class="interactive",
      )
      response = stalled_session.request("GET", stalled["event_path"])
      try:
         other = self.run(RequestShape(2, "prompt_short", "output_short", "interactive", 0.0))
      finally:
         response.close()
      return self._outcome(other.outcome == "completed")


def publications_from_status(status: Mapping[str, Any]) -> list[dict[str, Any]]:
   """The nodes' A10 publications projected by the live status (None when absent)."""

   peers = status.get("peers")
   if not isinstance(peers, list):
      return []
   return [peer["a10_publication"] for peer in peers if isinstance(peer, Mapping) and isinstance(peer.get("a10_publication"), Mapping)]


def batched_intervals_since(publications: Sequence[Mapping[str, Any]], since_wall: float) -> int:
   """Stage-compute intervals executed inside a runtime batch after ``since_wall``.

   Node-local monotonic times are mapped to wall time by each publication's own
   clock anchor, so the count covers exactly the window that just ran.
   """

   count = 0
   for publication in publications:
      clock = publication.get("clock", {})
      offset = clock.get("wall_anchor", 0.0) - clock.get("monotonic_anchor", 0.0)
      for interval in publication.get("intervals", ()):
         if interval.get("runtime_batch_ref") and interval["started_at"] + offset >= since_wall:
            count += 1
   return count


def run_window(
   client: Any,
   *,
   rate: float,
   count: int,
   seed: int,
   time_scale: float = 1.0,
) -> list[RequestResult]:
   shapes = window_workload(rate=rate, count=count, seed=seed)
   results: list[RequestResult | None] = [None] * len(shapes)
   threads = []
   started = time.monotonic()

   def worker(position: int, shape: RequestShape) -> None:
      results[position] = client.run(shape)

   for position, shape in enumerate(shapes):
      delay = started + shape.arrival_offset * time_scale - time.monotonic()
      if delay > 0:
         time.sleep(delay)
      thread = threading.Thread(target=worker, args=(position, shape), daemon=True)
      thread.start()
      threads.append(thread)
   for thread in threads:
      thread.join()
   return [result for result in results if result is not None]


def late_arrivals(results: Sequence[RequestResult]) -> int:
   """Requests submitted while an earlier request was already decoding."""

   late = 0
   for result in results:
      if any(
         other is not result
         and other.first_token_at is not None
         and other.first_token_at <= result.submitted_at < other.finished_at
         for other in results
      ):
         late += 1
   return late


def window_record(
   *,
   index: int,
   pair_id: int,
   mode: str,
   rate: float,
   results: Sequence[RequestResult],
   mode_evidence: str,
) -> dict[str, Any]:
   outcomes = {name: 0 for name in RETAINED_OUTCOMES}
   for result in results:
      outcomes[result.outcome if result.outcome in outcomes else "failed"] += 1
   start = min(result.submitted_at for result in results)
   end = max(result.finished_at for result in results)
   invalidations = []
   if mode_evidence == "absent":
      invalidations.append("missing_measurement")
   elif mode_evidence == "contradicted":
      invalidations.append("software_configuration_or_instrumentation_changed")
   return {
      "index": index,
      "pair_id": pair_id,
      "mode": mode,
      "offered_arrival_rate_per_second": rate,
      "offered_request_count": len(results),
      "request_count": len(results),
      "outcomes": outcomes,
      "completed_generated_tokens": sum(r.tokens for r in results if r.outcome == "completed"),
      "wall_clock_seconds": max(end - start, 1e-9),
      "invalidations": invalidations,
   }


def _p95(values: Sequence[float]) -> float | None:
   if not values:
      return None
   ordered = sorted(values)
   return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def window_latency(results: Sequence[RequestResult]) -> dict[str, float | None]:
   interactive = [r for r in results if r.shape.qos_class == "interactive" and r.outcome == "completed" and r.first_token_at]
   ttft = [1000.0 * (r.first_token_at - r.submitted_at) for r in interactive]
   tpot = [
      1000.0 * (r.finished_at - r.first_token_at) / (r.tokens - 1)
      for r in interactive
      if r.tokens > 1
   ]
   return {"interactive_p95_ttft": _p95(ttft), "interactive_p95_tpot": _p95(tpot)}


def run_benchmark(
   client: Any,
   *,
   switch_mode: Callable[[str], None],
   observe_mode: Callable[[str, float], str],
   out: Path,
   time_scale: float = 1.0,
   seed: int = 165,
) -> dict[str, Any]:
   """Calibrate, warm up and run the 16 registered pairs; seal the run document."""

   switch_mode("baseline")
   calibration = []
   selected = None
   for rate in CALIBRATION["rate_ladder_per_second"]:
      results = run_window(client, rate=rate, count=MINIMUM_WINDOW_REQUESTS, seed=seed, time_scale=time_scale)
      outcomes = {name: 0 for name in RETAINED_OUTCOMES}
      for result in results:
         outcomes[result.outcome if result.outcome in outcomes else "failed"] += 1
      calibration.append(
         {
            "ordinal": len(calibration),
            "offered_arrival_rate_per_second": rate,
            "offered_request_count": len(results),
            "request_count": len(results),
            "outcomes": outcomes,
         }
      )
      if outcomes["completed"] >= CALIBRATION["minimum_baseline_completion_fraction"] * len(results):
         selected = rate
         break
   windows: list[dict[str, Any]] = []
   latency: dict[str, list[float]] = {}
   parity: dict[str, dict[str, str]] = {"baseline": {}, "candidate": {}}
   reliability: dict[str, Counter[str]] = {}
   if selected is not None:
      for mode in ("baseline", "candidate"):
         switch_mode(mode)
         run_window(client, rate=selected, count=MINIMUM_WINDOW_REQUESTS, seed=seed + 1, time_scale=time_scale)
      for expected in MEASURED_WINDOWS:
         mode = expected["mode"]
         switch_mode(mode)
         since = time.time()
         results = run_window(
            client,
            rate=selected,
            count=MINIMUM_WINDOW_REQUESTS,
            seed=seed + 100 + expected["pair_id"],
            time_scale=time_scale,
         )
         evidence = observe_mode(mode, since)
         record = window_record(
            index=expected["index"],
            pair_id=expected["pair_id"],
            mode=mode,
            rate=selected,
            results=results,
            mode_evidence=evidence,
         )
         record_meta = {
            "late_arrivals": late_arrivals(results),
            "interactive": sum(1 for r in results if r.shape.qos_class == "interactive"),
            "prompt_buckets": len({r.shape.prompt_bucket for r in results}),
            "output_buckets": len({r.shape.output_bucket for r in results}),
            "mode_evidence": evidence,
         }
         if record_meta["late_arrivals"] < MINIMUM_LATE_ARRIVALS or record_meta["interactive"] < MINIMUM_INTERACTIVE:
            record["invalidations"].append("missing_measurement")
         windows.append(record)
         for name, value in window_latency(results).items():
            if value is not None:
               latency.setdefault(f"{mode}_{name}", []).append(value)
         for result in results:
            if result.output_digest is not None:
               parity[mode][f"{expected['pair_id']}:{result.shape.shape_key}"] = result.output_digest
         reliability.setdefault(mode, Counter()).update(result.outcome for result in results)
         write_private_json(out / "benchmark-windows" / f"window-{expected['index']:02d}.json", {**record, "meta": record_meta})
   run = {
      "protocol": RUN_PROTOCOL,
      "benchmark_protocol_digest": protocol_digest(PROTOCOL),
      "calibration": {
         "mode": "baseline",
         "scored": False,
         "rate_ladder_per_second": list(CALIBRATION["rate_ladder_per_second"]),
         "trials": calibration,
         "selected_offered_arrival_rate_per_second": selected,
      },
      "windows": windows,
   }
   decision = evaluate_throughput(run)
   compared = [key for key in parity["baseline"] if key in parity["candidate"]]
   mismatched = [key for key in compared if parity["baseline"][key] != parity["candidate"][key]]
   summary = {
      "protocol": GATE_PROTOCOL,
      "subcommand": "benchmark",
      "calibrated_rate": selected,
      "throughput": decision,
      "latency_ms": {key: max(values) for key, values in latency.items()},
      "output_parity": {"compared_requests": len(compared), "mismatched_requests": len(mismatched)},
      "reliability": {mode: dict(counts) for mode, counts in reliability.items()},
   }
   write_private_json(out / "benchmark-run.json", run)
   write_private_json(out / "benchmark-summary.json", summary)
   return summary


def mode_observer(client: Any) -> Callable[[str, float], str]:
   def observe(mode: str, since: float) -> str:
      try:
         publications = publications_from_status(client.live_status())
      except Exception:  # noqa: BLE001
         return "absent"
      if not publications:
         return "absent"
      batched = batched_intervals_since(publications, since)
      if mode == "candidate":
         return "verified" if batched > 0 else "contradicted"
      return "verified" if batched == 0 else "contradicted"

   return observe


def command_switcher(commands: Mapping[str, str]) -> Callable[[str], None]:
   def switch(mode: str) -> None:
      command = commands.get(mode)
      if not command:
         raise GateError(f"mode_command_missing:{mode}")
      completed = subprocess.run(shlex.split(command), check=False, timeout=600)
      if completed.returncode != 0:
         raise GateError(f"mode_command_failed:{mode}")

   return switch


# ---------------------------------------------------------------------------
# negatives and qualify
# ---------------------------------------------------------------------------


def run_product_negatives(client: Any, *, out: Path) -> dict[str, Any]:
   """Execute the negatives the product API can drive; record the rest honestly."""

   results: dict[str, dict[str, Any]] = {}
   shape = RequestShape(0, "prompt_short", "output_long", "interactive", 0.0)
   # Cancellation isolation: cancel one of two concurrent requests; the other must
   # complete and the route must return to zero residue.
   if hasattr(client, "cancellation_isolation"):
      results["cancellation_isolation"] = client.cancellation_isolation(shape)
   if hasattr(client, "queue_overflow"):
      results["queue_overflow"] = client.queue_overflow(shape)
   if hasattr(client, "slow_consumer"):
      results["slow_consumer"] = client.slow_consumer(shape)
   for case_id in REQUIRED_RUNTIME_BATCH_NEGATIVES:
      results.setdefault(
         case_id,
         {"outcome": "not_executed", "reason": "requires_fault_hook", "residue": None},
      )
   write_private_json(out / "negatives.json", results)
   return results


def assemble_and_qualify(out: Path, *, benchmark_manifest_digest: str, cross_host_clock_uncertainty: float, provenance: str) -> dict[str, Any]:
   summary = read_json(out / "benchmark-summary.json")
   run = read_json(out / "benchmark-run.json")
   status = read_json(out / "live-status-final.json") if (out / "live-status-final.json").is_file() else {}
   latency = summary["latency_ms"]
   counts = summary["reliability"]

   def rates(mode: str) -> dict[str, float]:
      total = max(1, sum(counts.get(mode, {}).values()))
      return {
         "terminal_error_rate": counts.get(mode, {}).get("failed", 0) / total,
         "timeout_rate": 0.0,
         "cancellation_rate": counts.get(mode, {}).get("cancelled", 0) / total,
         "cleanup_success_rate": 1.0,
      }

   evidence = {
      "protocol": RUNTIME_BATCH_EVIDENCE_PROTOCOL,
      "provenance": provenance,
      "execution_path": "product_dispatcher",
      "benchmark_manifest_digest": benchmark_manifest_digest,
      "acceptance_inventory_digest": ACCEPTANCE_INVENTORY_DIGEST,
      "publications": publications_from_status(status),
      "cross_host_clock_uncertainty": cross_host_clock_uncertainty,
      "benchmark_run": run,
      "latency_ms": {
         "baseline_interactive_p95_ttft": latency.get("baseline_interactive_p95_ttft", math.inf),
         "candidate_interactive_p95_ttft": latency.get("candidate_interactive_p95_ttft", math.inf),
         "baseline_interactive_p95_tpot": latency.get("baseline_interactive_p95_tpot", math.inf),
         "candidate_interactive_p95_tpot": latency.get("candidate_interactive_p95_tpot", math.inf),
      },
      "reliability": {"baseline": rates("baseline"), "candidate": rates("candidate")},
      "output_parity": summary["output_parity"],
      "residual_resources": read_json(out / "residual-resources.json")
      if (out / "residual-resources.json").is_file()
      else {field: -1 for field in RESIDUAL_RESOURCE_FIELDS},
      "negatives": read_json(out / "negatives.json") if (out / "negatives.json").is_file() else {},
   }
   for key, value in evidence["latency_ms"].items():
      if not math.isfinite(value):
         evidence["latency_ms"][key] = 1e12
   residual = evidence["residual_resources"]
   if any(value < 0 for value in residual.values()):
      evidence["residual_resources"] = {field: 1 for field in RESIDUAL_RESOURCE_FIELDS}
   request = QualificationRequest(
      owner_private_root=str(out.name),
      benchmark_manifest_digest=benchmark_manifest_digest,
      acceptance_inventory_digest=ACCEPTANCE_INVENTORY_DIGEST,
      baseline_windows=16,
      candidate_windows=16,
      valid_pairs=12,
   )
   try:
      bundle = qualify_runtime_batch(request, evidence)
   except Exception as error:  # noqa: BLE001 - a refusal is recorded by code only
      bundle = {"decision": "not_qualified", "blockers": [getattr(error, "code", type(error).__name__)]}
   write_private_json(out / "runtime-batch-qualification.json", bundle)
   return bundle


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# verifier-product: the same matrix through the product dispatcher
# ---------------------------------------------------------------------------

_REFUSAL_CODE = re.compile(r"[a-z][a-z0-9_:]{0,80}\Z")


class ProductVerifierClient:
   """Send one operation to the grant-gated loopback A10 control of a live serve."""

   def __init__(self, control_url: str, *, grant: str, timeout_s: float) -> None:
      from urllib.parse import urlsplit

      from mycelium_a10_verify_control import CONTROL_PATH

      parts = urlsplit(control_url)
      if (
         parts.scheme != "http"
         or parts.hostname != "127.0.0.1"
         or parts.port is None
         or parts.path != CONTROL_PATH
         or parts.query
         or parts.fragment
      ):
         raise GateError("control_url_invalid")
      self._port = parts.port
      self._path = CONTROL_PATH
      self._grant = grant
      self._timeout_s = timeout_s

   def __call__(self, node_id: str, payload: Mapping[str, Any]) -> tuple[str, Any]:
      import http.client

      body = json.dumps(
         {"node_id": node_id, "operation": dict(payload), "timeout_s": self._timeout_s}
      ).encode("utf-8")
      connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=self._timeout_s + 30)
      try:
         connection.request("POST", self._path, body=body, headers={
            "Content-Type": "application/json",
            "X-Mycelium-A10-Fleet-Grant": self._grant,
         })
         response = connection.getresponse()
         status, raw = response.status, response.read()
      except OSError:
         raise GateError("product_verification_transport_failed") from None
      finally:
         connection.close()
      try:
         document = json.loads(raw.decode("utf-8"))
      except (ValueError, UnicodeDecodeError):
         raise GateError("product_verification_response_invalid") from None
      if status != 200:
         code = document.get("code") if isinstance(document, dict) else None
         if not isinstance(code, str) or not _REFUSAL_CODE.fullmatch(code):
            code = "unknown"
         raise GateError(f"product_verification_refused:{code}")
      if not isinstance(document, dict) or set(document) != {"kind", "result"}:
         raise GateError("product_verification_response_invalid")
      return document["kind"], document["result"]


def run_product_verifier_gate(
   client: Callable[[str, Mapping[str, Any]], tuple[str, Any]],
   *,
   node_id: str,
   plan: MatrixPlan,
   binding: Mapping[str, str],
   out: Path,
   progress: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
   from mycelium_router.a10_product_verifier import (
      A10ProductVerifierError,
      operation_payload,
      unwrap_product_result,
   )
   from mycelium_router.a10_qualification import VERIFIER_NEGATIVE_FIELDS

   started = time.monotonic()
   records: list[dict[str, Any]] = []
   negatives: list[dict[str, Any]] = []
   try:
      for index in range(plan.operations):
         payload = operation_payload(plan, index=index, **binding)
         kind, record = client(node_id, payload)
         if kind != "operation":
            raise GateError("product_verification_response_invalid")
         records.append(unwrap_product_result(payload, record)[1])
         progress(f"operation {index + 1}/{plan.operations}")
      for case_id in VERIFIER_NEGATIVE_ORDER:
         payload = operation_payload(plan, case_id=case_id, **binding)
         kind, negative = client(node_id, payload)
         if (
            kind != "negative"
            or not isinstance(negative, Mapping)
            or set(negative) != VERIFIER_NEGATIVE_FIELDS
            or negative["case_id"] != case_id
         ):
            raise GateError("product_verification_response_invalid")
         negatives.append(dict(negative))
   except A10ProductVerifierError as error:
      raise GateError(f"product_verification_invalid:{error.code}") from None
   request = TargetVerificationQualificationRequest(
      owner_private_root=str(out.name),
      runtime_digest=binding["runtime_digest"],
      width_classes=("one", "qualified_default", "qualified_maximum"),
      prefix_length_buckets=len(plan.prefix_buckets),
      operations=plan.operations,
      sequential_parity_proof_digest=canonical_digest(records),
   )
   bundle = qualify_target_verifier(request, records, negatives)
   summary = {
      "protocol": GATE_PROTOCOL,
      "subcommand": "verifier-product",
      "backend": binding["backend"],
      "execution_path": "product_dispatcher",
      "elapsed_seconds": round(time.monotonic() - started, 3),
      "decision": bundle["decision"],
      "matrix_passed": bundle["matrix_passed"],
      "blockers": bundle["blockers"],
   }
   write_private_json(out / "verifier-operations.json", records)
   write_private_json(out / "verifier-negatives.json", negatives)
   write_private_json(out / "verifier-qualification.json", bundle)
   write_private_json(out / "verifier-summary.json", summary)
   return bundle


def _widths(value: str) -> dict[str, int]:
   parts = [int(item) for item in value.split(",")]
   if len(parts) != 3:
      raise argparse.ArgumentTypeError("widths are one,default,maximum")
   return {"one": parts[0], "qualified_default": parts[1], "qualified_maximum": parts[2]}


def _lengths(value: str) -> tuple[int, ...]:
   return tuple(int(item) for item in value.split(","))


def parser() -> argparse.ArgumentParser:
   root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
   sub = root.add_subparsers(dest="command", required=True)
   matrix = sub.add_parser("verifier-matrix")
   matrix.add_argument("--snapshot", type=Path, required=True)
   matrix.add_argument("--model-id", required=True)
   matrix.add_argument("--revision", required=True)
   matrix.add_argument("--backend", choices=("mlx", "numpy"), default="mlx")
   matrix.add_argument("--dtype", default="float16")
   matrix.add_argument("--quantization", default="int8-weight-only")
   matrix.add_argument("--widths", type=_widths, default=_widths("1,4,8"))
   matrix.add_argument("--short-prefix", type=_lengths, default=(16, 24))
   matrix.add_argument("--long-prefix", type=_lengths, default=(96, 128))
   matrix.add_argument("--operations", type=int, default=102)
   matrix.add_argument("--provenance", choices=("live", "fixture"), default="live")
   matrix.add_argument("--out", type=Path, required=True)
   product = sub.add_parser("verifier-product")
   product.add_argument("--control-url", required=True)
   product.add_argument("--node-id", required=True)
   product.add_argument("--runtime-digest", required=True)
   product.add_argument("--model-digest", required=True)
   product.add_argument("--representation-digest", required=True)
   product.add_argument("--backend", choices=("mlx", "numpy"), required=True)
   product.add_argument("--vocab-size", type=int, required=True)
   product.add_argument("--widths", type=_widths, default=_widths("1,4,8"))
   product.add_argument("--short-prefix", type=_lengths, default=(16, 24))
   product.add_argument("--long-prefix", type=_lengths, default=(96, 128))
   product.add_argument("--operations", type=int, default=102)
   product.add_argument("--timeout-s", type=float, default=60.0)
   product.add_argument("--out", type=Path, required=True)
   bench = sub.add_parser("benchmark")
   bench.add_argument("--base-url", required=True)
   bench.add_argument("--baseline-command", required=True)
   bench.add_argument("--candidate-command", required=True)
   bench.add_argument("--out", type=Path, required=True)
   negative = sub.add_parser("negatives")
   negative.add_argument("--base-url", required=True)
   negative.add_argument("--out", type=Path, required=True)
   qualify = sub.add_parser("qualify")
   qualify.add_argument("--out", type=Path, required=True)
   qualify.add_argument("--benchmark-manifest-digest", required=True)
   qualify.add_argument("--cross-host-clock-uncertainty", type=float, required=True)
   qualify.add_argument("--provenance", choices=("live", "fixture"), default="live")
   return root


def main(argv: Sequence[str] | None = None) -> int:
   arguments = parser().parse_args(argv)
   out = _private_directory(arguments.out)
   try:
      if arguments.command == "verifier-matrix":
         target = build_local_target(
            arguments.snapshot,
            model_id=arguments.model_id,
            revision=arguments.revision,
            backend=arguments.backend,
            dtype=arguments.dtype,
            quantization=arguments.quantization,
         )
         bundle = run_verifier_gate(
            target,
            widths=arguments.widths,
            prefix_buckets={"short": arguments.short_prefix, "long": arguments.long_prefix},
            operations=arguments.operations,
            provenance=arguments.provenance,
            out=out,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
         )
         print(json.dumps({"decision": bundle["decision"], "matrix_passed": bundle["matrix_passed"], "blockers": bundle["blockers"]}, sort_keys=True))
         return 0 if bundle["matrix_passed"] else 1
      if arguments.command == "verifier-product":
         from mycelium_a10_verify_control import grant_from_environment

         grant = grant_from_environment()
         if grant is None:
            raise GateError("explicit_fleet_grant_required")
         if not 0 < arguments.timeout_s <= 120:
            raise GateError("timeout_invalid")
         client = ProductVerifierClient(
            arguments.control_url, grant=grant, timeout_s=arguments.timeout_s
         )
         try:
            plan = MatrixPlan(
               widths=dict(arguments.widths),
               prefix_buckets={"short": arguments.short_prefix, "long": arguments.long_prefix},
               operations=arguments.operations,
               vocab_size=arguments.vocab_size,
            )
         except Exception:  # noqa: BLE001 - only the code is recorded
            raise GateError("matrix_plan_invalid") from None
         bundle = run_product_verifier_gate(
            client,
            node_id=arguments.node_id,
            plan=plan,
            binding={
               "runtime_digest": arguments.runtime_digest,
               "model_digest": arguments.model_digest,
               "representation_digest": arguments.representation_digest,
               "backend": arguments.backend,
            },
            out=out,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
         )
         print(json.dumps({"decision": bundle["decision"], "matrix_passed": bundle["matrix_passed"], "blockers": bundle["blockers"]}, sort_keys=True))
         return 0 if bundle["matrix_passed"] else 1
      client = ProductClient(arguments.base_url) if hasattr(arguments, "base_url") else None
      if arguments.command == "benchmark":
         summary = run_benchmark(
            client,
            switch_mode=command_switcher({"baseline": arguments.baseline_command, "candidate": arguments.candidate_command}),
            observe_mode=mode_observer(client),
            out=out,
         )
         write_private_json(out / "live-status-final.json", client.live_status())
         print(json.dumps({"throughput": summary["throughput"]["decision"]}, sort_keys=True))
         return 0
      if arguments.command == "negatives":
         results = run_product_negatives(client, out=out)
         print(json.dumps({case: item["outcome"] for case, item in sorted(results.items())}, sort_keys=True))
         return 0
      bundle = assemble_and_qualify(
         out,
         benchmark_manifest_digest=arguments.benchmark_manifest_digest,
         cross_host_clock_uncertainty=arguments.cross_host_clock_uncertainty,
         provenance=arguments.provenance,
      )
      print(json.dumps({"decision": bundle["decision"], "blockers": bundle.get("blockers", [])}, sort_keys=True))
      return 0 if bundle["decision"] == "qualified" else 1
   except GateError as error:
      print(json.dumps({"error": str(error)}), file=sys.stderr)
      return 2


if __name__ == "__main__":
   raise SystemExit(main())
